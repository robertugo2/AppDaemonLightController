"""
LightController - controls light according to programmed scenes based on multiple inputs
like switches, motions sensors and door/window sensors

For more info read README.md
"""
import appdaemon.plugins.hass.hassapi as hass
import appdaemon.plugins.mqtt.mqttapi as mqtt
import json
import time

# Constants
# States
OFF = 'OFF'
COLD = 'COLD'
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

        # Sun state processing. Used in determining default light on scene.
        self.listen_state(self.on_sun, 'sun.sun')
        self.sun_state = self.get_state('sun.sun').lower()

        # Configure time based scene selector. Used in addition to sun based processing.
        if self.args.get("cold_scene_time"):
            self.cold_scene_time_start = self.args["cold_scene_time_period"]["start"]
            self.cold_scene_time_end = self.args["cold_scene_time_period"]["end"]
        else:
            self.cold_scene_time_start = "06:50:00"
            self.cold_scene_time_end = "19:00:00"
        self.run_daily(self.on_time, self.cold_scene_time_start, random_start=5, random_end=10)
        self.run_daily(self.on_time, self.cold_scene_time_end, random_start=5, random_end=10)

        # Listen state of light
        # State of light is buffered to speed up execution time
        self.listen_state(self.on_light, self.light_entity, attribute='all')
        self.current_state = ''
        self.current_state = self.detect_state()

        # Motion sensors
        self.motion_sensors = {}
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
            self.timer = None
        else:
            self.motion_sensors = None

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

        # Select default scene
        self.process_default_scene()

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

        if contact_status is False and self.current_state == OFF:
            self.select_scene(self.default_scene)
            self.log("Light on due to %s contact sensor" % contact_name)

    def on_occupancy_change(self, event_name, data, kwargs):
        motion_sensor = kwargs['motion_sensor']
        sensor_name = motion_sensor['name']
        payload = json.loads(data['payload'])
        occupancy = payload.get('occupancy', None)
        if occupancy is None:
            # Payload does not contain occupancy data
            return
        if self.motion_sensors[sensor_name] == occupancy:
            # No change
            return
        self.log('Occupancy change for %s to %s' % (sensor_name, str(occupancy)))
        self.motion_sensors[sensor_name] = occupancy

        # Actual motion processing
        if self.current_state == OFF and occupancy is True and motion_sensor['turn_on']:
            # Turn on the light
            self.select_scene(self.default_scene, transition=1)
            self.log("Light on due to %s motion sensor" % sensor_name)
        self.process_light_timeout()

    def process_light_timeout(self):
        if self.motion_sensors is None:
            return
        all_motion_sensors_off = all([v is False for v in self.motion_sensors.values()])
        if self.timer is not None and (self.current_state == OFF or not all_motion_sensors_off):
            self.cancel_timer(self.timer)
            self.timer = None
            self.log('Timer stop')
        if self.timer is None and self.current_state != OFF and all_motion_sensors_off:
            self.timer = self.run_in(self.on_timer, self.motion_timeout)
            self.log('Timer start')

    def on_timer(self, kwargs):
        self.timer = None
        self.select_scene(OFF, transition=5)
        self.log('Timer timeout')

    def process_default_scene(self):
        if (self.sun_state != BELOW_HORIZON) and self.now_is_between(self.cold_scene_time_start,
                                                                     self.cold_scene_time_end):
            self.default_scene = COLD
        else:
            self.default_scene = WARM
        self.log('Setting default scene to %s' % str(self.default_scene))

        if self.auto_color_temp_change:
            if self.current_state == COLD and self.default_scene == WARM:
                self.select_scene(WARM, 30)
            if self.current_state == WARM and self.default_scene == COLD:
                self.select_scene(COLD, 30)

    def on_click(self, event_name, data, kwargs):
        entity = kwargs['switch']
        payload = json.loads(data['payload'])
        event = payload.get('action', None)
        if not event:
            return
        self.log("Action '%s' from '%s'" % (event, entity))
        now = time.time()
        if now - self.last_command < self.debounce:
            self.log('Command debounced')
            return
        switch = self.switches[entity]
        if event == switch[SINGLE]:
            if self.current_state == OFF:
                self.select_scene(self.default_scene)
            else:
                self.select_scene(OFF)
        if event == switch[DOUBLE]:
            if self.current_state == WARM:
                self.select_scene(COLD)
            else:
                self.select_scene(WARM)
        if event == switch[HOLD]:
            if self.current_state != DIMM:
                self.select_scene(DIMM)
        self.last_command = time.time()

    def on_light(self, entity, attribute, old, new, kwargs):
        self.current_state = self.detect_state()
        self.process_light_timeout()

    def on_time(self, kwargs):
        self.process_default_scene()

    def on_sun(self, entity, attribute, old, new, kwargs):
        self.log('Detected sun status change')
        self.current_state = self.detect_state()
        self.sun_state = new.lower()
        self.process_default_scene()

    def detect_state(self):
        state = self.get_state(self.light_entity)
        if state.upper() == OFF:
            detected_state = OFF
            if self.current_state != detected_state:
                self.log('state=%s' % detected_state)
        elif self.color_temp_support:
            brightness = self.get_state(self.light_entity, attribute=BRIGHTNESS)
            color_temp = self.get_state(self.light_entity, attribute=COLOR_TEMP)
            if (brightness == self.scene_cold[BRIGHTNESS] or brightness == (
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
            brightness = self.get_state(self.light_entity, attribute=BRIGHTNESS)
            if brightness == self.scene_cold[BRIGHTNESS] or brightness == (self.scene_cold[BRIGHTNESS] - 1):
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

    def select_scene(self, scene, transition=0):
        self.log('Changing scene to %s' % scene)
        if scene == OFF:
            self.my_turn_off(transition=transition)
        if self.color_temp_support:
            if scene == COLD:
                self.my_turn_on(transition=transition, color_temp=self.scene_cold[COLOR_TEMP],
                                brightness=self.scene_cold[BRIGHTNESS])
            if scene == WARM:
                self.my_turn_on(transition=transition, color_temp=self.scene_warm[COLOR_TEMP],
                                brightness=self.scene_warm[BRIGHTNESS])
            if scene == DIMM:
                self.my_turn_on(transition=transition, color_temp=self.scene_dimm[COLOR_TEMP],
                                brightness=self.scene_dimm[BRIGHTNESS])
        else:
            if scene == COLD:
                self.my_turn_on(transition=transition, brightness=self.scene_cold[BRIGHTNESS])
            if scene == WARM:
                self.my_turn_on(transition=transition, brightness=self.scene_warm[BRIGHTNESS])
            if scene == DIMM:
                self.my_turn_on(transition=transition, brightness=self.scene_dimm[BRIGHTNESS])

    def my_turn_on(self, **kwargs):
        if self.mqtt_entity:
            kwargs['state'] = 'ON'
            msg = json.dumps(kwargs)
            self.mqtt_publish(topic="zigbee2mqtt/%s/set" % self.mqtt_entity, payload=msg, namespace='mqtt')
        else:
            self.turn_on(self.light_entity, **kwargs)

    def my_turn_off(self, **kwargs):
        if self.mqtt_entity:
            kwargs['state'] = 'OFF'
            msg = json.dumps(kwargs)
            self.mqtt_publish(topic="zigbee2mqtt/%s/set" % self.mqtt_entity, payload=msg, namespace='mqtt')
        else:
            self.turn_off(self.light_entity, **kwargs)
