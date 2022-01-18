Light Controller
================
[AppDaemon](https://appdaemon.readthedocs.io/) app - Controls light according to programmed scenes based on multiple inputs
like switches, motions sensors and door/window sensors.
Suitable for users, that have smart light always power on and zigbee switches.
Needs to be used with [HomeAssistant](https://www.home-assistant.io/)

Main features:
* Supports 3 build-in, configurable scenes
    - cold - used during daylight
    - warm - used for afternoon
    - dimmed - as a night light
* Speed - where it is possible, state is cached to speed up calculation of response.
* Commands are sent and responses are received via mqtt, but light can be controlled via HA as well
* Configurable warm/cold scene time periods
* Support motions sensors
* Support contact sensors
* Smart functionality for combining all sensors and switches

Minimal configuration:
```yaml
# Name of app instance
light_ctrl:
    # Module name
    module: lightcontroller
    # Module class
    class: LightController
    # Define light entity to control.
    light_entity: light.light_1
    # Define z2m names of switches.
    # Switch needs to support at least single click, ideally double and hold as well
    switches:
        - name: switch_1
```

Notes:
* z2m main topic needs to de default one (zigbee2mqtt)

Switch event behavior:
* single - turn off or turn on default scene (warm or cold depending on current time)
* double - when off or not warm - set to warm. If warm - set to cold. Convenient way to change scene.
* hold   - set dimmed scene

Full configuration:
```yaml
# Name of app instance
light_ctrl:
    # Module name
    module: lightcontroller
    # Module class
    class: LightController
    # Scenes configuration. If not defined, some defaults will be used.
    scene_cold:
        color_temp: 250
        brightness: 255
    scene_warm:
        color_temp: 389
        brightness: 150
    scene_dimm:
        color_temp: 500
        brightness: 1
    # If light don't support color_temp, then it is needed to set following flag to false:
    color_temp_support: True
    # Suppress multiple clicks/commands/service calls for x seconds:
    debounce: 1
    # Define HA light entity to control. Still needed, even if z2m entity is defined.
    light_entity: light.light_1
    # It is possible to define z2m name topic to control light,
    # then command will be send directly to z2m via mqtt:
    mqtt_entity: light_1
    # Define input switches as z2m names with associated 'action' attribute value.
    # If given switch don't support double/hold action, then some functionality will be reduced.
    switches:
        - name: switch_1
          single: "left_single"
          double: "left_double"
          hold: "left_hold"
    # Define additional time bounds for cold scene.
    # If not defined, default values below will be used
    cold_scene_time:
        start: "07:00:00"
        end: "19:00:00"
    # If you want to automatically change the light color on time boundaries defined upper, 
    # then set following flag:
    auto_color_temp_change: True
    # Define time bounds for warm scene, if you want to have default dimmed scene instead of warn on defined time boundaries. 
    # No auto transition happens here.
    warm_scene_time:
        start: "05:00:00"
        end: "22:00:00"
    # Define z2m motion sensors to define auto on function and timeout function
    motion_sensors:
        - name: occupancy_1
          turn_on: True # defult True, defines if given motion sensor can trigger light on action
                        # if set to false, it will be used only to check, if timeout can be started to count
    # Timeout for motion sensors, that is after which time light should be turned off after not detecting a move.
    motion_timeout: 300  # seconds
    # Set time for which movement will be ignored after manually turning off light
    ignore_motion_after_turn_off_time = 5
    # When light is going off due to no movement detection, 
    # define time that will turn on light after movement detection without transition time
    power_off_cancel_timeout = 8
    # Define transition time for turning off light after motion detection timeout
    motion_power_off_transition_time = 5
    # Contacts, that can trigger light on acion when contact=false.
    # To be used with door sensor, so when door goes open, light will turn on.
    contacts:
        - name: contact_1
```
It is also possible to control light via HA events:
* Name: `lightctrl.set`
* Data:
```yaml
light: some_name # Define name of light to set. Can be 'all' as well to set all lights
lights: # You can define list of lights to control instead of defining one light
  - light1
  - light2
# Action to perform, can be: turn_on, turn_off, toggle, set_scene
action: some_action
# If action is 'set_scene', it is needed to define scene to set. Can be OFF, DIMM, WARM, COLD
scene: DIMM
```