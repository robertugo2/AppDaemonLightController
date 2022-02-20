"""
LightController - controls light according to programmed scenes based on multiple inputs
like switches, motions sensors and door/window sensors

For more info read README.md
"""
import appdaemon.plugins.hass.hassapi as hass
import appdaemon.plugins.mqtt.mqttapi as mqtt
import json
import time

# Constant
# States
OFF = 'OFF'
ON = 'ON'
COLD = 'COLD'
MOTION_DIMMED = 'MOTION_DIMMED'
WARM = 'WARM'
DIMM = 'DIMM'
UNDEFINED = 'UNDEFINED'
# Light
BRIGHTNESS = 'brightness'
COLOR_TEMP = 'color_temp'
# Switch
SINGLE = 'single'
HOLD = 'hold'
DOUBLE = 'double'
# Sun
BELOW_HORIZON = 'below_horizon'
ABOVE_HORIZON = 'above_horizon'


class LightController(hass.Hass, mqtt.Mqtt):
    def initialize(self):
        """
        Load configuration.
        """
        # Supported function
        self.color_temp_support = self.args.get('color_temp_support', True)
        self.auto_color_temp_change = self.args.get('auto_color_temp_change', True)

        # Control entities
        self.light_entity = self.args['light_entity']
        self.mqtt_entity = self.args.get('mqtt_entity', None)

        # Scenes
        self.scene_cold = self.args.get('scene_cold', dict())
        self.scene_warm = self.args.get('scene_warm', dict())
        self.scene_dimm = self.args.get('scene_dimm', dict())

        # Get brightness for scenes
        self.scene_cold[BRIGHTNESS] = self.scene_cold.get(BRIGHTNESS, 255)
        self.scene_warm[BRIGHTNESS] = self.scene_warm.get(BRIGHTNESS, 255)
        self.scene_dimm[BRIGHTNESS] = self.scene_dimm.get(BRIGHTNESS, 76)

        # Get color temp for scenes (if supported)
        if self.color_temp_support:
            self.scene_cold[COLOR_TEMP] = self.scene_cold.get(COLOR_TEMP, 250)
            self.scene_warm[COLOR_TEMP] = self.scene_warm.get(COLOR_TEMP, 389)
            self.scene_dimm[COLOR_TEMP] = self.scene_dimm.get(COLOR_TEMP, 400)

        # Config print
        self.log('Config for %s' % self.light_entity)
        self.log('scene_cold: %s' % str(self.scene_cold))
        self.log('scene_warm: %s' % str(self.scene_warm))
        self.log('scene_dimm: %s' % str(self.scene_dimm))

        # Command debounce
        self.debounce = self.args.get('debounce', 1.0)  # debounce time in seconds
        self.last_command = 0  # indicates last switch action timestamp

        # Switches
        self.log("Defined switches:")
        self.switches = dict()
        for switch in self.args['switches']:
            # Subscribe to topic to receive messages
            self.mqtt_subscribe("zigbee2mqtt/%s" % switch['name'], namespace='mqtt')
            # Listen to events related to given switch
            self.listen_event(self.on_click, "MQTT_MESSAGE", namespace='mqtt', topic="zigbee2mqtt/%s" % switch['name'],
                              switch=switch['name'])
            # Process remaining config options
            switch[SINGLE] = switch.get(SINGLE, SINGLE)
            switch[HOLD] = switch.get(HOLD, HOLD)
            switch[DOUBLE] = switch.get(DOUBLE, DOUBLE)
            # Save switch config
            self.switches[switch['name']] = switch
            # Log switch configuration
            self.log('Input: %s' % switch)

        # Configure time based scene selector. Used in addition to sun based processing.
        if self.args.get("cold_scene_time"):
            self.cold_scene_time = self.args['cold_scene_time']
        else:
            self.cold_scene_time = {
                'start': "06:50:00",
                'end': "19:00:00"
            }
        self.run_daily(self.on_time, self.cold_scene_time['start'], random_start=5, random_end=10)
        self.run_daily(self.on_time, self.cold_scene_time['end'], random_start=5, random_end=10)

        # For dimm scene, we don't want to have default behavior
        if self.args.get("warm_scene_time"):
            self.warm_scene_time = self.args['warm_scene_time']
            self.run_daily(self.on_time, self.warm_scene_time['start'], random_start=5, random_end=10)
            self.run_daily(self.on_time, self.warm_scene_time['end'], random_start=5, random_end=10)
        else:
            self.warm_scene_time = None

        # Motion sensors
        self.motion_sensors = {}
        self.ignore_motion_after_turn_off_time = self.args.get('ignore_motion_after_turn_off_time', 5)
        self.last_turn_off_due_to_switch = 0
        if self.args.get('motion_sensors'):
            self.log('Adding motions sensors')
            self.motion_timeout = self.args.get("motion_timeout", 5 * 60)
            self.log('Motion timeout %d' % self.motion_timeout)
            for sensor in self.args['motion_sensors']:
                sensor['turn_on'] = sensor.get('turn_on', True)
                self.motion_sensors[sensor['name']] = False  # Assume some initial condition - no motion
                self.mqtt_subscribe("zigbee2mqtt/%s" % sensor['name'], namespace='mqtt')
                self.listen_event(self.on_occupancy_change, "MQTT_MESSAGE", namespace='mqtt',
                                  topic="zigbee2mqtt/%s" % sensor['name'],
                                  motion_sensor=sensor)
                self.log('Input %s' % str(sensor))
        else:
            self.motion_sensors = None
        self.timer = None
        self.power_off_cancel_timeout = self.args.get('power_off_cancel_timeout', 8)
        self.motion_power_off_transition_time = self.args.get('motion_power_off_transition_time', 5)
        self.brightness_dimmed_light = self.args.get('brightness_dimmed_light', 8)

        self.contacts = {}
        if self.args.get('contacts'):
            self.log("Adding contacts")
            for contact in self.args['contacts']:
                self.contacts[contact['name']] = True  # closed
                self.mqtt_subscribe("zigbee2mqtt/%s" % contact['name'], namespace='mqtt')
                self.listen_event(self.on_contact, "MQTT_MESSAGE", namespace='mqtt',
                                  topic="zigbee2mqtt/%s" % contact['name'],
                                  contact=contact)
                self.log('Input %s' % str(contact))
        else:
            self.contacts = None

        # Listen state of light
        # State of light is buffered to speed up execution time
        self.listen_state(self.on_light, self.light_entity, attribute='all')
        self.current_state = ''
        self.current_state = self.detect_state()

        # Select default scene
        self.process_default_scene()

        # Subscribe to HA event
        self.listen_event(self.process_event, event="lightctrl.set")

    # Process external events like from the HA
    def process_event(self, event, data, kwargs):
        acceptable_names = {'all', self.light_entity, self.mqtt_entity}
        # Below, please have in mind, that mqtt_entity can be None, so default value for 'light' can't be None
        if (data.get('light', 'NONE') in acceptable_names) or any(
                n in data.get('lights', {}) for n in acceptable_names):
            self.log("Event received %s" % str(data))
            action = data.get('action', None)
            transition = data.get('transition', 0)
            if action == 'turn_on':
                if self.current_state == OFF or self.is_motion_dimm_running:
                    self.select_scene(self.default_scene, transition)
                    self.log("Turn ON the light due to event")
            elif action == 'turn_off':
                if self.current_state != OFF:
                    self.select_scene(OFF, transition)
                    self.log("Turn OFF the light due to event")
            elif action == 'set_scene' and data.get('scene', None) in {OFF, WARM, COLD, DIMM}:
                scene = data['scene']
                self.select_scene(scene)
                self.log("Select %s scene due to event" % scene)
            elif action == 'toggle':
                self.toggle_light()
            else:
                raise Exception('Incorrect action %s' % action)

    def on_contact(self, event_name, data, kwargs):
        contact = kwargs['contact']
        contact_name = contact['name']
        payload = json.loads(data['payload'])
        contact_status = payload.get('contact', None)

        if contact_status is None:
            # Payload does not contain occupancy data
            return
        if self.contacts[contact_name] == contact_status:
            # No change
            return
        self.contacts[contact_name] = contact_status

        # Some change occurred
        if contact_status is False and (self.current_state == OFF or self.is_motion_dimm_running):
            self.select_scene(self.default_scene)
            self.log("Light on due to %s contact sensor" % contact_name)

    # Callback for motion sensors.
    # Determine, if there is any change in occupancy status for monitored devices
    def on_occupancy_change(self, event_name, data, kwargs):

        motion_sensor = kwargs['motion_sensor']
        sensor_name = motion_sensor['name']
        payload = json.loads(data['payload'])
        occupancy = payload.get('occupancy', None)

        # Payload does not contain occupancy data
        if occupancy is None:
            return

        # No change detected
        if self.motion_sensors[sensor_name] == occupancy:
            return

        self.log('Occupancy change for %s to %s' % (sensor_name, str(occupancy)))
        self.motion_sensors[sensor_name] = occupancy

        # --- Actual motion processing, as some change was detected ---

        # (Light is off or timer is running) and motion is detected.
        # So all actions below runs only, when motion is detected.
        # Actions related to light change are executed in 'process_light_timeout' function as well,
        # that is called during light state change as well.
        if (self.current_state == OFF or self.is_motion_dimm_running) and occupancy is True:

            # Do not turn off light, when time from last turn off command via switch
            # is less than 'ignore_motion_after_turn_off_time'.
            if (time.time() - self.last_turn_off_due_to_switch) < self.ignore_motion_after_turn_off_time:
                self.log('Light on due to motion detection ignored, '
                         'as it is too close from turn off command via switch')

            # If lights are dimmed, turn them on again.
            # If timer is running in other state, then no action is needed here and timer will be canceled
            # in 'process_light_timeout' function call;
            elif self.is_motion_dimm_running:
                self.select_scene(self.default_scene, transition=0, force=True)
                self.log("Light on due to %s motion sensor and light in dimmed state" % sensor_name)

            # Simply turn on the light
            elif motion_sensor['turn_on']:
                self.select_scene(self.default_scene, transition=1)
                self.log("Light on due to %s motion sensor (turn_on flag enabled)" % sensor_name)
        self.process_light_timeout()

    # Check, if timer is running in dimmed state.
    @property
    def is_motion_dimm_running(self):
        if self.timer is None:
            return False
        exec_time, interval, kwargs = self.info_timer(self.timer)
        return kwargs.get('state', None) == MOTION_DIMMED

    # This function is called, when the motion status is changed or the light state is changed.
    # Propose of this function is the timer control only.
    # Rest functionality related to motion processing (like light control) is done in timer timeout function
    # and the 'on_occupancy_change' callback.
    def process_light_timeout(self):

        # If no motion sensors are defined, exit.
        if self.motion_sensors is None:
            return

        # Helper value to check, if in general any motion is detected.
        all_motion_sensors_off = all([v is False for v in self.motion_sensors.values()])

        if self.timer is not None and (self.current_state == OFF or not all_motion_sensors_off):
            # If timer is running and scene is OFF (so it was turned off externally) or
            # motion is detected again (so we don't want to do any action via timer), then cancel timer.
            # Note: If motion was detected during dimmed state, then it will be turned on via 'on_occupancy_change'
            self.cancel_timer(self.timer)
            self.timer = None
            self.log('Timer stop')

        # Timer is running in dimmed state, but state is not dimmed. Check, if it is due to transition time
        if self.is_motion_dimm_running and self.current_state != MOTION_DIMMED:
            exec_time, interval, kwargs = self.info_timer(self.timer)
            # If state is different from undefined one (that can occur during transition time)
            # or time period is not in power off transition time
            # Note: ... plus condition from upper level, that state is not in desired one
            if self.current_state != UNDEFINED \
                    or not ((time.time() - kwargs['started']) > (self.motion_power_off_transition_time + 1)):
                # Timer is canceled, but if no motion is detected,
                # then timer will be started over in next if statement
                self.cancel_timer(self.timer)
                self.timer = None
                self.log('Timer stop due to incorrect state in timer dimmed state')

        # If timer is not running (so motion timeout is not running), light is on and there is no motion detected,
        # then start timeout timer.
        if self.timer is None and self.current_state != OFF and all_motion_sensors_off:
            self.timer = self.run_in(self.on_timer, self.motion_timeout)
            self.log('Timer start')

    # Process timer timeout
    def on_timer(self, kwargs):
        if kwargs.get('state', None) == MOTION_DIMMED:
            # Turn off lights
            self.timer = None
            self.select_scene(OFF)
            self.current_state = OFF  # set current scene here due to race condition
            self.log('Timer timeout: Select OFF scene')
        else:
            # Change scene to dimmed one and start timer again in dimmed mode/state
            self.select_scene(MOTION_DIMMED, transition=self.motion_power_off_transition_time)
            self.current_state = MOTION_DIMMED  # set current scene here due to race condition
            self.timer = self.run_in(self.on_timer, self.power_off_cancel_timeout, state=MOTION_DIMMED,
                                     started=time.time())
            self.log('Timer timeout: Select MOTION_DIMMED scene')

    # Select default light scene and change light temperature for lights, that are on (if enabled).
    def process_default_scene(self):

        if self.now_is_between(self.cold_scene_time['start'], self.cold_scene_time['end']):
            self.default_scene = COLD
        # If warm scene time is not defined, then WARM scene is default outside of COLD scene time
        elif self.warm_scene_time is None or self.now_is_between(self.warm_scene_time['start'],
                                                                 self.warm_scene_time['end']):
            self.default_scene = WARM
        else:
            self.default_scene = DIMM
        self.log('Setting default scene to %s' % str(self.default_scene))

        # Change color temperature for lights, that are on. Don't do that for WARM-DIMM transition.
        if self.auto_color_temp_change:
            if self.current_state == COLD and self.default_scene == WARM:
                self.select_scene(WARM, 30)
            if self.current_state == WARM and self.default_scene == COLD:
                self.select_scene(COLD, 30)

    # Process mqtt payload from switch
    def on_click(self, event_name, data, kwargs):
        # Get action (if any in payload)
        entity = kwargs['switch']
        payload = json.loads(data['payload'])
        event = payload.get('action', None)
        if not event:
            return
        self.log("Action '%s' from '%s'" % (event, entity))
        switch = self.switches[entity]

        # Select action
        if event == switch[SINGLE]:
            self.toggle_light()
        if event == switch[DOUBLE]:
            if self.current_state == WARM:
                self.select_scene(COLD)
            else:
                self.select_scene(WARM)
        if event == switch[HOLD]:
            if self.current_state != DIMM:
                self.select_scene(DIMM)

    # Toggle light (ON-OFF)
    def toggle_light(self):
        if self.current_state == OFF:
            self.select_scene(self.default_scene)
        else:
            self.select_scene(OFF)
            # Store time of last off command due to switch, as it is needed to debounce motion detection on event,
            # as we don't want to turn on light shortly after turning it off via switch
            # (but not after motion re-detection).
            self.last_turn_off_due_to_switch = time.time()

    # Callback for light state changes
    def on_light(self, entity, attribute, old, new, kwargs):
        self.current_state = self.detect_state()
        self.process_light_timeout()

    # Callback for time based triggers - for default scene change during a day
    def on_time(self, kwargs):
        self.log("Time triggered default scene processing.")
        self.process_default_scene()

    #
    def detect_state(self):
        state = self.get_state(self.light_entity)
        brightness = self.get_state(self.light_entity, attribute=BRIGHTNESS)
        if state.upper() == OFF:
            detected_state = OFF
            if self.current_state != detected_state:
                self.log('state=%s' % detected_state)
        elif brightness == self.brightness_dimmed_light and self.is_motion_dimm_running:
            # Lets detected MOTION_DIMMED state based on brightness and timer status.
            # Otherwise, continue detection.
            detected_state = MOTION_DIMMED
            if self.current_state != detected_state:
                self.log('state=%s, brightness=%s' % (detected_state, str(brightness)))
        elif self.color_temp_support:
            color_temp = self.get_state(self.light_entity, attribute=COLOR_TEMP)
            if brightness == 0:
                detected_state = OFF
            elif (brightness == self.scene_cold[BRIGHTNESS] or brightness == (
                    self.scene_cold[BRIGHTNESS] - 1)) and color_temp == self.scene_cold[COLOR_TEMP]:
                detected_state = COLD
            elif (brightness == self.scene_warm[BRIGHTNESS] or brightness == (
                    self.scene_warm[BRIGHTNESS] - 1)) and color_temp == self.scene_warm[COLOR_TEMP]:
                detected_state = WARM
            elif (brightness == self.scene_dimm[BRIGHTNESS] or brightness == (
                    self.scene_dimm[BRIGHTNESS] - 1)) and color_temp == self.scene_dimm[COLOR_TEMP]:
                detected_state = DIMM
            else:
                detected_state = UNDEFINED
            if self.current_state != detected_state:
                self.log('state=%s, brightness=%s, color_temp=%s' % (detected_state, str(brightness), str(color_temp)))
        else:
            if brightness == 0:
                detected_state = OFF
            elif brightness == self.scene_cold[BRIGHTNESS] or brightness == (self.scene_cold[BRIGHTNESS] - 1):
                detected_state = COLD
            elif brightness == self.scene_warm[BRIGHTNESS] or brightness == (self.scene_warm[BRIGHTNESS] - 1):
                detected_state = WARM
            elif brightness == self.scene_dimm[BRIGHTNESS] or brightness == (self.scene_dimm[BRIGHTNESS] - 1):
                detected_state = DIMM
            else:
                detected_state = UNDEFINED
            if self.current_state != detected_state:
                self.log('state=%s, brightness=%s' % (detected_state, str(brightness)))
        return detected_state

    def select_scene(self, scene, transition=0, force=False):
        self.log('Changing scene to %s' % scene)
        now = time.time()
        # 'transition < 5' condition is for long transitions, like changing default scene.
        if ((now - self.last_command) < self.debounce) and (transition < 5) and (not force):
            self.log('Command debounced')
            return
        if scene == OFF:
            self.light_turn_off(transition=transition)
        elif scene == ON:
            self.light_turn_on(transition=transition)
        elif scene == MOTION_DIMMED:
            # For MOTION_DIMMED change brightness only, as it will preserve color temperature,
            # that we don't want to change.
            self.light_turn_on(transition=transition, brightness=self.brightness_dimmed_light)
        elif self.color_temp_support:
            if scene == COLD:
                self.light_turn_on(transition=transition, color_temp=self.scene_cold[COLOR_TEMP],
                                   brightness=self.scene_cold[BRIGHTNESS])
            elif scene == WARM:
                self.light_turn_on(transition=transition, color_temp=self.scene_warm[COLOR_TEMP],
                                   brightness=self.scene_warm[BRIGHTNESS])
            elif scene == DIMM:
                self.light_turn_on(transition=transition, color_temp=self.scene_dimm[COLOR_TEMP],
                                   brightness=self.scene_dimm[BRIGHTNESS])
            else:
                raise Exception('Unrecognized scene to set %s' % str(scene))
        else:
            if scene == COLD:
                self.light_turn_on(transition=transition, brightness=self.scene_cold[BRIGHTNESS])
            elif scene == WARM:
                self.light_turn_on(transition=transition, brightness=self.scene_warm[BRIGHTNESS])
            elif scene == DIMM:
                self.light_turn_on(transition=transition, brightness=self.scene_dimm[BRIGHTNESS])
            else:
                raise Exception('Unrecognized scene to set %s' % str(scene))
        self.last_command = time.time()

    def light_turn_on(self, **kwargs):
        if self.mqtt_entity:
            kwargs['state'] = 'ON'
            msg = json.dumps(kwargs)
            self.mqtt_publish(topic="zigbee2mqtt/%s/set" % self.mqtt_entity, payload=msg, namespace='mqtt')
        else:
            self.turn_on(self.light_entity, **kwargs)

    def light_turn_off(self, **kwargs):
        if self.mqtt_entity:
            kwargs['state'] = 'OFF'
            msg = json.dumps(kwargs)
            self.mqtt_publish(topic="zigbee2mqtt/%s/set" % self.mqtt_entity, payload=msg, namespace='mqtt')
        else:
            self.turn_off(self.light_entity, **kwargs)
