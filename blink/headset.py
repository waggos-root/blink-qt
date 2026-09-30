
"""Call control for USB headsets that implement the HID Telephony usage page (e.g. Yealink WH6x)"""

import glob
import os
import sys
import time

from PyQt6.QtCore import QSocketNotifier, QTimer

from application import log
from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.configuration.settings import SIPSimpleSettings

from blink.util import run_in_gui_thread


__all__ = ['HeadsetManager']


# Usages are (usage_page << 16) | usage_id
TELEPHONY_HOOK_SWITCH = 0x000B0020
TELEPHONY_FLASH       = 0x000B0021
TELEPHONY_REDIAL      = 0x000B0024
TELEPHONY_PHONE_MUTE  = 0x000B002F
TELEPHONY_RINGER      = 0x000B009E
TELEPHONY_PHONE_KEY_0 = 0x000B00B0
LED_MUTE              = 0x00080009
LED_OFF_HOOK          = 0x00080017
LED_RING              = 0x00080018
LED_HOLD              = 0x00080020
LED_MICROPHONE        = 0x00080021

# The descriptor maps key pad array values 0-11 to Phone Key 0-9, * and #, but
# headsets (e.g. the Yealink WH66) send 0 for no key and shift the rest by one
KEYPAD_KEYS = {index + 1: key for index, key in enumerate('0123456789*#')}


class HIDField(object):
    def __init__(self, report_id, offset, size, usage, relative):
        self.report_id = report_id
        self.offset = offset
        self.size = size
        self.usage = usage
        self.relative = relative

    def extract(self, data):
        value = int.from_bytes(data, 'little') >> self.offset
        return value & ((1 << self.size) - 1)


class HIDReportDescriptor(object):
    """Minimal HID report descriptor parser that only keeps variable (non-array) data fields"""

    def __init__(self, descriptor):
        self.inputs = {}         # usage -> HIDField
        self.outputs = {}        # usage -> HIDField
        self.output_sizes = {}   # report_id -> size in bits
        self.uses_report_ids = False
        self._parse(descriptor)

    def _parse(self, data):
        usage_page = report_id = report_size = report_count = 0
        global_stack = []
        usages = []
        usage_minimum = None
        bit_offsets = {}  # (main tag, report_id) -> bit offset
        position = 0
        while position < len(data):
            prefix = data[position]
            if prefix == 0xFE:  # long item
                position += 3 + data[position+1] if position + 1 < len(data) else len(data)
                continue
            size = (0, 1, 2, 4)[prefix & 0x03]
            item_type = (prefix >> 2) & 0x03
            tag = prefix >> 4
            value = int.from_bytes(data[position+1:position+1+size], 'little')
            position += 1 + size
            if item_type == 1:  # global
                if tag == 0:
                    usage_page = value
                elif tag == 7:
                    report_size = value
                elif tag == 8:
                    report_id = value
                    self.uses_report_ids = True
                elif tag == 9:
                    report_count = value
                elif tag == 10:
                    global_stack.append((usage_page, report_id, report_size, report_count))
                elif tag == 11 and global_stack:
                    usage_page, report_id, report_size, report_count = global_stack.pop()
            elif item_type == 2:  # local
                if tag == 0:
                    usages.append((value, size))
                elif tag == 1:
                    usage_minimum = (value, size)
                elif tag == 2 and usage_minimum is not None:
                    usages.extend((usage, usage_minimum[1]) for usage in range(usage_minimum[0], value + 1))
                    usage_minimum = None
            elif item_type == 0:  # main
                if tag in (8, 9):  # input, output
                    key = tag, report_id
                    offset = bit_offsets.get(key, 0)
                    is_constant = value & 0x01
                    is_variable = value & 0x02
                    is_relative = value & 0x04
                    fields = self.inputs if tag == 8 else self.outputs
                    usages = [usage | (usage_page << 16) if usage_size < 4 else usage for usage, usage_size in usages]
                    if is_variable and not is_constant and usages:
                        for index in range(report_count):
                            usage = usages[min(index, len(usages) - 1)]
                            fields.setdefault(usage, HIDField(report_id, offset + index*report_size, report_size, usage, bool(is_relative)))
                    elif not is_constant and usages:  # array, keyed by its first usage and holding the index of the active usage
                        fields.setdefault(usages[0], HIDField(report_id, offset, report_size, usages[0], bool(is_relative)))
                    bit_offsets[key] = offset + report_size * report_count
                    if tag == 9:
                        self.output_sizes[report_id] = bit_offsets[key]
                usages = []
                usage_minimum = None


class HIDTelephonyDevice(object):
    def __init__(self, path, name, descriptor):
        self.path = path
        self.name = name
        self.descriptor = descriptor
        self.fd = None

    @classmethod
    def find(cls):
        for sysfs_path in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
            try:
                with open(os.path.join(sysfs_path, 'device', 'report_descriptor'), 'rb') as f:
                    descriptor = HIDReportDescriptor(f.read())
                with open(os.path.join(sysfs_path, 'device', 'uevent')) as f:
                    uevent = dict(line.strip().partition('=')[::2] for line in f)
            except (OSError, ValueError, IndexError):
                continue
            if TELEPHONY_HOOK_SWITCH not in descriptor.inputs or LED_OFF_HOOK not in descriptor.outputs:
                continue
            path = os.path.join('/dev', os.path.basename(sysfs_path))
            if not os.access(path, os.R_OK | os.W_OK):
                log.warning('Found headset %s at %s but it is not accessible (check udev permissions)' % (uevent.get('HID_NAME', 'unknown'), path))
                continue
            return cls(path, uevent.get('HID_NAME', path), descriptor)
        return None

    def open(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read(self):
        """Return a {usage: value} mapping for the input fields contained in the received report"""
        data = os.read(self.fd, 256)
        if not data:
            raise OSError('device disconnected')
        report_id = 0
        if self.descriptor.uses_report_ids:
            report_id, data = data[0], data[1:]
        return {usage: field.extract(data) for usage, field in self.descriptor.inputs.items() if field.report_id == report_id}

    def write(self, values):
        """Write the {usage: value} output fields, grouped by report"""
        reports = {}
        for usage, value in values.items():
            field = self.descriptor.outputs.get(usage)
            if field is None:
                continue
            reports[field.report_id] = reports.get(field.report_id, 0) | ((value & ((1 << field.size) - 1)) << field.offset)
        for report_id, report in reports.items():
            data = report.to_bytes((self.descriptor.output_sizes[report_id] + 7) // 8, 'little')
            if self.descriptor.uses_report_ids:
                data = bytes([report_id]) + data
            os.write(self.fd, data)


@implementer(IObserver)
class HeadsetManager(object, metaclass=Singleton):
    """
    Keeps the headset call indicators in sync with the audio sessions and
    maps the headset buttons to call actions.

    The host owns the hook state: it sets the Off-Hook LED when a call is in
    progress and the headset echoes it back as the Hook Switch input. A Hook
    Switch change that does not match what we set is a button press on the
    headset (answer when ringing, hang up when in a call). The echo can arrive
    up to a second later (DECT headsets need to bring up the radio link), so
    the expected echoes are remembered until they arrive or time out. If the
    hook state still disagrees with our calls after that (e.g. the call button
    was pressed but no call was placed), it is forced back in sync.
    """

    rescan_interval = 5000
    echo_interval = 3
    dial_timeout = 30  # seconds after the last key press when a partially dialed number is discarded
    hook_sync_interval = 1500

    def __init__(self):
        self.started = False
        self.device = None
        self.notifier = None
        self.hook_state = False  # last hook switch state reported by the device
        self.pending_echoes = {}  # hook state -> time we asked the device to switch to it
        self.output_state = None
        self.buttons = {}
        self.pressed_key = None
        self.dialed_number = ''
        self.last_key_time = 0
        self.rescan_timer = QTimer()
        self.rescan_timer.setInterval(self.rescan_interval)
        self.rescan_timer.timeout.connect(self._open_device)
        self.hook_sync_timer = QTimer()
        self.hook_sync_timer.setSingleShot(True)
        self.hook_sync_timer.setInterval(self.hook_sync_interval)
        self.hook_sync_timer.timeout.connect(self._SH_HookSyncTimerTimeout)

    def start(self):
        if self.started or not sys.platform.startswith('linux'):
            return
        self.started = True
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkCallStateDidChange')
        notification_center.add_observer(self, name='BlinkSessionDidChangeState')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        self._open_device()
        if self.device is None:
            self.rescan_timer.start()

    def stop(self):
        if not self.started:
            return
        self.started = False
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='BlinkCallStateDidChange')
        notification_center.remove_observer(self, name='BlinkSessionDidChangeState')
        notification_center.remove_observer(self, name='CFGSettingsObjectDidChange')
        self.rescan_timer.stop()
        self.hook_sync_timer.stop()
        if self.device is not None:
            try:
                self.device.write(dict.fromkeys(self.device.descriptor.outputs, 0))
            except OSError:
                pass
        self._close_device()

    def _open_device(self):
        device = HIDTelephonyDevice.find()
        if device is None:
            return
        try:
            device.open()
        except OSError as e:
            log.warning('Failed to open headset %s: %s' % (device.path, e))
            return
        log.info('Using headset %s (%s) for call control' % (device.name, device.path))
        self.rescan_timer.stop()
        self.device = device
        self.hook_state = False
        self.pending_echoes = {}
        self.output_state = None
        self.buttons = {}
        self.pressed_key = None
        self.dialed_number = ''
        self.last_key_time = 0
        self.notifier = QSocketNotifier(device.fd, QSocketNotifier.Type.Read)
        self.notifier.activated.connect(self._SH_DeviceReadable)
        self.update()

    def _close_device(self):
        self.hook_sync_timer.stop()
        if self.notifier is not None:
            self.notifier.setEnabled(False)
            self.notifier.deleteLater()
            self.notifier = None
        if self.device is not None:
            self.device.close()
            self.device = None

    def _device_lost(self, error):
        log.info('Headset %s disconnected (%s)' % (self.device.name, error))
        self._close_device()
        self.rescan_timer.start()

    @property
    def calls(self):
        from blink.sessions import SessionManager
        return [session for session in SessionManager().sessions if session.state in ('connecting/*', 'connected/*') and {'audio', 'video'}.intersection(session.streams.types)]

    @property
    def incoming_calls(self):
        from blink.sessions import IncomingRequest, SessionManager
        return [request for request in SessionManager().incoming_requests if isinstance(request, IncomingRequest) and not request.proposal and request.stream_types.intersection({'audio', 'video'})]

    def update(self):
        if self.device is None:
            return
        calls = self.calls
        ringing = bool(self.incoming_calls)
        off_hook = bool(calls)
        on_hold = off_hook and all(session.local_hold for session in calls)
        settings = SIPSimpleSettings()
        muted = off_hook and settings.audio.muted
        audible_ring = ringing and not settings.audio.silent  # in silent mode only the ring LED signals the call
        state = {LED_OFF_HOOK: off_hook, LED_RING: ringing, TELEPHONY_RINGER: audible_ring, LED_HOLD: on_hold, LED_MUTE: muted, LED_MICROPHONE: off_hook and not muted}
        if state != self.output_state:
            self._write(state)
        self._check_hook_state()

    def _write(self, state):
        try:
            self.device.write(state)
        except OSError as e:
            self._device_lost(e)
        else:
            if state[LED_OFF_HOOK] != self.hook_state:
                self.pending_echoes[state[LED_OFF_HOOK]] = time.monotonic()
            self.output_state = state

    def _check_hook_state(self):
        if self.device is not None and self.output_state is not None and self.hook_state != self.output_state[LED_OFF_HOOK] and not self.hook_sync_timer.isActive():
            self.hook_sync_timer.start()

    def _SH_HookSyncTimerTimeout(self):
        if self.device is None or self.output_state is None:
            return
        off_hook = self.output_state[LED_OFF_HOOK]
        if self.hook_state == off_hook:
            return
        if time.monotonic() - self.pending_echoes.get(off_hook, float('-inf')) < self.echo_interval:
            self.hook_sync_timer.start()  # the device has yet to confirm our last change
            return
        # The device may only react to changes of the off-hook LED, so first match its hook state and then set ours
        state = self.output_state
        self._write({**state, LED_OFF_HOOK: self.hook_state})
        if self.device is not None:
            self._write(state)

    def _SH_DeviceReadable(self):
        try:
            values = self.device.read()
        except BlockingIOError:
            return
        except OSError as e:
            self._device_lost(e)
            return
        if not values:
            return
        off_hook = bool((self.output_state or {}).get(LED_OFF_HOOK))
        if TELEPHONY_HOOK_SWITCH in values:
            hook_state = bool(values[TELEPHONY_HOOK_SWITCH])
            if hook_state != self.hook_state:
                self.hook_state = hook_state
                is_echo = time.monotonic() - self.pending_echoes.pop(hook_state, float('-inf')) < self.echo_interval
                if hook_state and not off_hook and not is_echo:
                    if self.incoming_calls:
                        self._answer()
                    else:
                        self._dial()
                elif not hook_state and off_hook and not is_echo:
                    self._hangup()
                self._check_hook_state()
        for usage, action in ((TELEPHONY_PHONE_MUTE, self._toggle_mute), (TELEPHONY_FLASH, self._toggle_hold), (TELEPHONY_REDIAL, self._redial)):
            if usage not in values:
                continue
            pressed = bool(values[usage])
            if pressed and not self.buttons.get(usage, False):
                action()
            self.buttons[usage] = pressed
        if TELEPHONY_PHONE_KEY_0 in values:
            key = KEYPAD_KEYS.get(values[TELEPHONY_PHONE_KEY_0])
            if key is not None and key != self.pressed_key:
                self._key_pressed(key)
            self.pressed_key = key

    def _answer(self):
        incoming_calls = self.incoming_calls
        if incoming_calls:
            incoming_calls[0].dialog.accept()  # requests are kept sorted by priority

    def _dial(self):
        from blink.contacts import URIUtils
        from blink.sessions import SessionManager, StreamDescription
        number, self.dialed_number = self.dialed_number, ''
        if not number:
            return
        self._show_dialed_number('')
        if time.monotonic() - self.last_key_time > self.dial_timeout:
            return
        contact, contact_uri = URIUtils.find_contact(number)
        SessionManager().create_session(contact, contact_uri, [StreamDescription('audio')])

    def _key_pressed(self, key):
        from blink.sessions import SessionManager
        calls = self.calls
        if calls:
            session = SessionManager().active_session
            if session in calls and session.state == 'connected/*':
                session.send_dtmf(key)
        elif not self.incoming_calls:
            now = time.monotonic()
            if now - self.last_key_time > self.dial_timeout:
                self.dialed_number = ''
            self.last_key_time = now
            self.dialed_number += key
            self._show_dialed_number(self.dialed_number)

    def _show_dialed_number(self, number):
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().main_window.search_box.setText(number)

    def _hangup(self):
        from blink.sessions import SessionManager
        calls = self.calls
        if not calls:
            return
        active_session = SessionManager().active_session
        if active_session in calls:
            session = active_session
        else:
            session = next((session for session in calls if not session.local_hold), calls[0])
        session.end()

    def _toggle_mute(self):
        if not self.calls:
            return
        settings = SIPSimpleSettings()
        settings.audio.muted = not settings.audio.muted
        settings.save()

    def _toggle_hold(self):
        from blink.sessions import SessionManager
        session = SessionManager().active_session
        if session is None or session not in self.calls or session.state != 'connected/*':
            return
        if session.local_hold:
            session.unhold()
        else:
            session.hold()

    def _redial(self):
        from PyQt6.QtWidgets import QApplication
        if not self.calls:
            QApplication.instance().main_window.redial_action.trigger()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkCallStateDidChange(self, notification):
        self.update()

    def _NH_BlinkSessionDidChangeState(self, notification):
        self.update()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if {'audio.muted', 'audio.silent'}.intersection(notification.data.modified):
            self.update()

