import collections
import logging

from twisted.internet import reactor

from tron import job
from tron import action
from tron import command_context
from tron.utils import state
from tron.utils import timeutils

log = logging.getLogger(__name__)

class Error(Exception): pass

class InvalidStateError(Error): pass

class ServiceInstance(object):
    class ServiceInstanceState(state.NamedEventState): pass
    STATE_DOWN = ServiceInstanceState("down")
    STATE_UP = ServiceInstanceState("up")
    STATE_FAILED = ServiceInstanceState("failed", stop=STATE_DOWN, up=STATE_UP)
    STATE_STOPPING = ServiceInstanceState("stopping", down=STATE_DOWN)
    STATE_MONITORING = ServiceInstanceState("monitoring", down=STATE_FAILED, stop=STATE_STOPPING, up=STATE_UP)
    STATE_STARTING = ServiceInstanceState("starting", down=STATE_FAILED, monitor=STATE_MONITORING, stop=STATE_STOPPING)

    STATE_UNKNOWN = ServiceInstanceState("unknown", monitor=STATE_MONITORING)
    STATE_MONITORING['monitor_fail'] = STATE_UNKNOWN

    STATE_UP['stop'] = STATE_STOPPING
    STATE_UP['monitor'] = STATE_MONITORING
    STATE_DOWN['start'] = STATE_STARTING
    
    def __init__(self, service, node, instance_number):
        self.service = service
        self.instance_number = instance_number
        self.node = node

        self.id = "%s.%s" % (service.name, self.instance_number)
        
        self.machine = state.StateMachine(ServiceInstance.STATE_DOWN)
        
        self.context = command_context.CommandContext(self, service.context)
        
        self.monitor_action = None
        self.start_action = None
        self.kill_action = None
 
    @property
    def state(self):
        return self.machine.state

    @property
    def listen(self):
        return self.machine.listen
    
    @property
    def pid_file(self):
        if self.service.pid_file_template:
            try:
                return self.service.pid_file_template % self.context
            except KeyError:
                log.error("Failed to render pid file template: %r" % self.service.pid_file_template)
        else:
            log.warning("No pid_file configured for service %s", self.service.name)

        return None
    
    @property
    def command(self):
        try:
            return self.service.command % self.context
        except KeyError:
            log.error("Failed to render service command for service %s: %s", self.service.name, self.service.command)

        return None
    
    def _queue_monitor(self):
        self.monitor_action = None
        if self.service.monitor_interval > 0:
            reactor.callLater(self.service.monitor_interval, self._run_monitor)

    def _run_monitor(self):
        if self.monitor_action:
            log.warning("Monitor action already exists, old callLater ?")
            return
        
        self.machine.transition("monitor")
        pid_file = self.pid_file
        
        if pid_file is None:
            # If our pid file doesn't exist or failed to be generated, we can't really monitor
            self._monitor_complete_failstart()
            return
        
        monitor_command = "cat %(pid_file)s | xargs kill -0" % self.context

        self.monitor_action = action.ActionCommand("%s.monitor" % self.id, monitor_command)
        self.monitor_action.machine.listen(action.ActionCommand.COMPLETE, self._monitor_complete_callback)
        self.monitor_action.machine.listen(action.ActionCommand.FAILSTART, self._monitor_complete_failstart)

        self.node.run(self.monitor_action)
        # TODO: Need a timer on this in case the monitor hangs

    def _monitor_complete_callback(self):
        """Callback when our monitor has completed"""
        assert self.monitor_action
        self.last_check = timeutils.current_time()
        log.debug("Monitor callback with exit %r", self.monitor_action.exit_status)
        if self.monitor_action.exit_status != 0:
            self.machine.transition("down")
        else:
            self.machine.transition("up")
            self._queue_monitor()

        self.monitor_action = None

    def _monitor_complete_failstart(self):
        """Callback when our monitor failed to even start"""
        self.machine.transition("monitor_fail")
        self._queue_monitor()

        self.monitor_action = None
        
    def start(self):
        if self.machine.state != self.STATE_DOWN:
            raise InvalidStateError("Instance must be marked DOWN to start")

        self.machine.transition("start")

        command = self.command
        if command is None:
            self._start_complete_failstart()
            return

        self.start_action = action.ActionCommand("%s.start" % self.id, command)
        self.start_action.machine.listen(action.ActionCommand.COMPLETE, self._start_complete_callback)
        self.start_action.machine.listen(action.ActionCommand.FAILSTART, self._start_complete_failstart)

        self.node.run(self.start_action)
    
    def _start_complete_callback(self):
        if self.start_action.exit_status != 0:
            self.machine.transition("down")
        elif self.machine.state == self.STATE_STOPPING:
            # Someone tried to stop us while we were just getting going. 
            # Go ahead and kick of the kill operation now that we're up.
            self.kill_instance()
        else:
            self._queue_monitor()

        self.start_action = None

    def _start_complete_failstart(self):
        log.warning("Failed to start service %s (%s)", self.id, self.node.hostname)
        self.machine.transition("down")
        self.start_action = None

    def stop(self):       
        self.machine.transition("stop")
                
        if self.machine.state == self.STATE_STOPPING:
            self.kill_instance()

    def kill_instance(self):
        assert self.pid_file, self.pid_file
        
        kill_command = "cat %(pid_file)s | xargs kill" % self.context

        self.stop_action = action.ActionCommand("%s.stop" % self.id, kill_command)
        self.stop_action.machine.listen(action.ActionCommand.COMPLETE, self._stop_complete_callback)
        self.stop_action.machine.listen(action.ActionCommand.FAILSTART, self._stop_complete_failstart)

        self.node.run(self.stop_action)

    def _stop_complete_callback(self):
        if self.stop_action.exit_status != 0:
            log.error("Failed to stop service instance %s: Exit %r", self.id, self.stop_action.exit_status)

        self._queue_monitor()
        self.stop_action = None

    def _stop_complete_failstart(self):
        log.warning("Failed to start kill command for %s", self.id)
        self._queue_monitor()


class Service(object):
    class ServiceState(state.NamedEventState): pass
    STATE_DOWN = ServiceState("down")
    STATE_UP = ServiceState("up")
    STATE_DEGRADED = ServiceState("degraded")
    STATE_STOPPING = ServiceState("stopping", all_down=STATE_DOWN)
    STATE_FAILED = ServiceState("failed")
    STATE_STARTING = ServiceState("starting", all_up=STATE_UP, failed=STATE_DEGRADED, stop=STATE_STOPPING)
    
    STATE_DOWN['start'] = STATE_STARTING
    STATE_DEGRADED.update(dict(stop=STATE_STOPPING, all_up=STATE_UP, all_failed=STATE_FAILED)) 
    STATE_FAILED.update(dict(stop=STATE_STOPPING, up=STATE_DEGRADED, start=STATE_STARTING))
    STATE_UP.update(dict(stop=STATE_STOPPING, failed=STATE_DEGRADED, down=STATE_DEGRADED))
    
    def __init__(self, name=None, command=None, node_pool=None, context=None):
        self.name = name
        self.command = command
        self.scheduler = None
        self.node_pool = node_pool
        self.count = 0
        self.monitor_interval = None
        self.restart_interval = None
        self._restart_timer = None
        
        self.machine = state.StateMachine(Service.STATE_DOWN)

        self.pid_file_template = None

        self.context = None
        if context is not None:
            self.set_context(None)

        self.instances = []

    @property
    def state(self):
        if self.machine:
            return self.machine.state
        else:
            return None

    @property
    def listen(self):
        return self.machine.listen

    @property
    def is_started(self):
        """Indicate if the service has been started/initialized
        
        For now we're going to decide this if we have instances or not. It doesn't really
        coorespond well to a "state", but it might at some point need to be some sort of enable/disable thing.
        """
        return len(self.instances) > 0

    def set_context(self, context):
        self.context = command_context.CommandContext(self, context)

    def _clear_failed_instances(self):
        """Remove and cleanup any instances that are no longer with us"""
        self.instances = [inst for inst in self.instances if inst.state != ServiceInstance.STATE_FAILED]

    def _restart_after_failure(self):
        if self._restart_timer is None:
            return

        if self.state in (self.STATE_DEGRADED, self.STATE_FAILED):
            log.info("Restarting failed instances for service %s", self.name)
            self.start()
        else:
            self._restart_timer = None
    
    def start(self):    
        # Clear out the restart timer, just to make sure we don't get any extraneous starts
        self._restart_timer = None
        
        # Start can really mean restart any failed or down instances.
        # So first off, clear out any old instances that are of no use to us
        # anymore
        self._clear_failed_instances()

        # Build all the new instances well need
        needed_instances_count = self.count - len(self.instances)
        if needed_instances_count > 0:
            for _ in range(0, needed_instances_count):
                self.build_instance()
            
        self.machine.transition("start")
    
    def stop(self):
        self.machine.transition("stop")

        for service_instance in self.instances:
            service_instance.stop()
        
        # Just in case we somehow ended up stuck with no instances, double
        # check here for stop complete.
        if self.state == self.STATE_STOPPING and not self.instances:
            self.machine.transition("all_down")

    def _create_instance(self, node, instance_number):
        service_instance = ServiceInstance(self, node, instance_number)
        self.instances.append(service_instance)
        self.instances.sort(key=lambda i:i.instance_number)
        
        service_instance.listen(True, self._instance_change)

        return service_instance
    
    def _find_unused_instance_number(self):
        available_instance_numbers = set(range(0, self.count)) - set(instance.instance_number for instance in self.instances)
        if len(available_instance_numbers) == 0:
            return None
        return min(available_instance_numbers)

    def build_instance(self):
        node = self.node_pool.next_round_robin()

        instance_number = self._find_unused_instance_number()
        if instance_number is None:
            log.error("Can't build a new service instance for %r. %d instances in use. Maybe try again later ?", self.name, self.count)
            return None

        service_instance = self._create_instance(node, instance_number)

        # No reason not to start this guy right away, we don't keep 'down' instances 
        # around really.
        service_instance.start()

        return service_instance
    
    def _instance_change(self):
        """Handle any changes to our service's instances

        This is the state change callback handler for all our instances. 
        Anytime an instance changes, we need to re-evaluate our own current state.
        """
        # Remove any downed instances
        self.instances = [inst for inst in self.instances if inst.state != inst.STATE_DOWN]
        
        # Now we can make some inferences about state changes based on our instances
        if not self.instances:
            self.machine.transition("all_down")
        elif any([instance.state == ServiceInstance.STATE_FAILED for instance in self.instances]):
            self.machine.transition("failed")
            if all([instance.state == ServiceInstance.STATE_FAILED for instance in self.instances]):
                self.machine.transition("all_failed")
        elif len(self.instances) < self.count:
            log.info("Only found %d instances rather than %d", len(self.instances), self.count)
            self.machine.transition("down")
        elif all([instance.state == ServiceInstance.STATE_UP for instance in self.instances]):
            self.machine.transition("all_up")
        
        if self.machine.state in (Service.STATE_DEGRADED, Service.STATE_FAILED):
            # Start a restart timer if configure
            if self.restart_interval is not None and not self._restart_timer:
                self._restart_timer = reactor.callLater(self.restart_interval, self._restart_after_failure)

    def absorb_previous(self, prev_service):
        # Some changes we need to worry about:
        # * Changing instance counts
        # * Changing the command
        # * Changing the node pool
        # * Changes to the context ?
        # * Restart counts for downed services ?
                
        rebuild_all_instances = any([
                                     self.command != prev_service.command,
                                     self.scheduler != prev_service.scheduler
                                    ])
        
        # Since we are inheriting all the existing instances, 
        # it's safe to also inherit the previous state machine as well.
        self.machine = prev_service.machine

        # To permanently disable the older service, remove it's machine.
        prev_service.machine = None

        # Copy over all the old instances
        self.instances += prev_service.instances
        for service_instance in prev_service.instances:
            service_instance.machine.clear_listeners()
            service_instance.machine.listen(True, self._instance_change)
        
            if rebuild_all_instances:
                # For some configuration changes, we'll just stop all the previous instances.
                # When those services stop, we should be in a degraded mode, triggering a restart of
                # the newer generation of instances.
                service_instance.stop()
                removed_instances += 1

        prev_service.instances = []

        self.instances.sort(key=lambda i:i.instance_number)

        current_instances = [i for i in self.instances if i.state not in 
            (ServiceInstance.STATE_STOPPING, ServiceInstance.STATE_DOWN, ServiceInstance.STATE_FAILED)]

        # We have special handling for node pool changes.
        # This would cover the case of removing (or subsituting) a node in a pool
        # which would require rebalancing services.
        removed_instances = 0
        if self.node_pool != prev_service.node_pool:
            # How many instances per node should we have ?
            optimal_instances_per_node = self.count / len(self.node_pool.nodes)
            instance_count_per_node = collections.defaultdict(int)

            for service_instance in current_instances:
                # First we'll stop any instances on nodes that are no longer part of our pool
                try:
                    service_instance.node = self.node_pool[service_instance.node.hostname]
                except KeyError:
                    log.info("Stopping instance %r because it's not on a current node (%r)", service_instance.id, service_instance.node.hostname)
                    service_instance.stop()
                    removed_instances += 1    
                    continue
                
                instance_count_per_node[service_instance.node] += 1
                if instance_count_per_node[service_instance.node] > optimal_instances_per_node:
                    log.info("Stopping instance %r because node %s has too many instances", service_instance.id, service_instance.node.hostname)
                    service_instance.stop()
                    removed_instances += 1
                    continue

        
        current_instances = [i for i in self.instances if i.state not in 
            (ServiceInstance.STATE_STOPPING, ServiceInstance.STATE_DOWN, ServiceInstance.STATE_FAILED)]

        count_to_remove = (len(self.instances) - removed_instances) - self.count
        if count_to_remove > 0:
            instances_to_remove = current_instances[-count_to_remove:]
            for service_instance in instances_to_remove:                
                service_instance.stop()
                removed_instances += 1

        # Now make adjustments to how many there are
        while len(self.instances) < self.count:
            self.build_instance()

    @property
    def data(self):
        data = {
            'state': str(self.machine.state),     
        }
        data['instances'] = []
        for instance in self.instances:
            service_data = {
                'node': instance.node.hostname,
                'instance_number': instance.instance_number,
                'state': str(instance.state),
            }

            data['instances'].append(service_data)

        return data
        
    def restore(self, data):
        """Restore state of this service from datafile"""
        # The state of a service is more easier than for jobs. There are just a few things we want to guarantee:
        #  1. If service instances are up, they can continue to be up. We'll just start monitoring from where we left off.
        #  2. Failures are maintained and have to be cleared.
        
        # Start our machine from where it left off
        self.machine.state = state.named_event_by_name(Service.STATE_DOWN, data['state'])

        if self.machine.state == Service.STATE_DOWN:
            return

        # Restore all the instances
        # We're going to just indicate they are up and start a monitor
        for instance in data['instances']:
            try:
                node = self.node_pool[instance['node']]
            except KeyError:
                log.error("Failed to find node %s in pool for %s", instance['node'], self.name)
                continue
            service_instance = self._create_instance(node, instance['instance_number'])
            
            service_instance.machine.state = ServiceInstance.STATE_MONITORING
            service_instance._run_monitor()            
        
        self.instances.sort(key=lambda i:i.instance_number)