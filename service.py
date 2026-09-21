# -*- coding: utf-8 -*-
"""Picture in Picture for Kodi.

Shift+P (pressed in Kodi) hands the currently playing stream to a small,
borderless, always-on-top mpv window that does not take keyboard focus.

The same mpv window stays open across episodes: when Kodi auto-plays the next
item, it is loaded into the existing window, so the position and size you gave
it are kept. Shift+P again (or middle-click on the PiP window) closes it.
"""
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote_plus

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON_ID = 'service.pip'
ADDON_NAME = 'Picture in Picture'

MODE_MUTE, MODE_PAUSE = 0, 1

KEYMAP_XML = """<keymap>
  <global>
    <keyboard>
      <p mod="shift">NotifyAll(service.pip,toggle)</p>
    </keyboard>
  </global>
</keymap>
"""

# Extra mpv key bindings for the PiP window: middle-click closes it.
INPUT_CONF = "MBTN_MID quit\n"

# mpv --geometry x%:y% placement, indexed by the "corner" setting.
CORNERS = ('98%:96%', '2%:96%', '98%:4%', '2%:4%')

PLAYABLE_SCHEMES = {
    'http', 'https', 'rtmp', 'rtmps', 'rtmpt', 'rtsp', 'rtp', 'udp', 'tcp',
    'file', 'ftp', 'sftp', 'smb', 'mms', 'mmsh', 'mmst', 'srt', 'rist',
}

MPV_CANDIDATES = (
    '/usr/bin/mpv', '/usr/local/bin/mpv', '/opt/homebrew/bin/mpv',
    '/snap/bin/mpv', '/Applications/mpv.app/Contents/MacOS/mpv',
    r'C:\Program Files\mpv\mpv.exe', r'C:\Program Files (x86)\mpv\mpv.exe',
    r'C:\ProgramData\chocolatey\bin\mpv.exe',
    os.path.expandvars(r'%LOCALAPPDATA%\Programs\mpv\mpv.exe'),
    os.path.expandvars(r'%USERPROFILE%\scoop\apps\mpv\current\mpv.exe'),
)


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[%s] %s' % (ADDON_ID, msg), level)


def notify(msg):
    xbmcgui.Dialog().notification(ADDON_NAME, msg, xbmcgui.NOTIFICATION_INFO, 4000)


def popen_extras():
    # Hide the console window on Windows.
    return {'creationflags': 0x08000000} if os.name == 'nt' else {}


# Kodi helpers

def rpc(method, params=None):
    req = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params is not None:
        req['params'] = params
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(req)))
    except ValueError:
        return {}


def kodi_muted():
    res = rpc('Application.GetProperties', {'properties': ['muted']})
    return bool(res.get('result', {}).get('muted'))


def set_kodi_mute(state):
    rpc('Application.SetMute', {'mute': bool(state)})


# Pure helpers

def parse_kodi_url(raw):
    """Split Kodi's 'url|Header=value&Header2=value' syntax."""
    if '|' not in raw:
        return raw, {}
    url, _, tail = raw.partition('|')
    headers = {}
    for pair in tail.split('&'):
        if '=' in pair:
            key, value = pair.split('=', 1)
            headers[unquote_plus(key)] = unquote_plus(value)
    return url, headers


def split_headers(headers):
    """Return (user_agent, referrer, [other 'Key: value' header fields])."""
    ua = referrer = ''
    fields = []
    for key, value in headers.items():
        low = key.lower()
        if low == 'user-agent':
            ua = value
        elif low in ('referer', 'referrer'):
            referrer = value
        else:
            fields.append('%s: %s' % (key, value))
    return ua, referrer, fields


def header_args(headers):
    ua, referrer, fields = split_headers(headers)
    args = []
    if ua:
        args.append('--user-agent=' + ua)
    if referrer:
        args.append('--referrer=' + referrer)
    args += ['--http-header-fields-append=' + f for f in fields]
    return args


def resolve_source(raw):
    """Return (url_or_path, headers, error)."""
    url, headers = parse_kodi_url(raw)
    if url.startswith('special://'):
        return xbmcvfs.translatePath(url), headers, None
    match = re.match(r'^([a-zA-Z][a-zA-Z0-9+.\-]*)://', url)
    if match and match.group(1).lower() not in PLAYABLE_SCHEMES:
        return None, headers, 'mpv cannot open %s:// sources' % match.group(1)
    return url, headers, None


def find_mpv(addon):
    custom = addon.getSettingString('mpv_path').strip()
    if custom:
        custom = xbmcvfs.translatePath(custom)
        if os.path.isfile(custom):
            return custom
    found = shutil.which('mpv')
    if found:
        return found
    for candidate in MPV_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def mpv_option_text(mpv):
    try:
        return subprocess.run(
            [mpv, '--list-options'], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            timeout=15, **popen_extras()).stdout.decode('utf-8', 'replace')
    except Exception as exc:
        log('mpv --list-options failed: %s' % exc, xbmc.LOGWARNING)
        return ''


def has_option(text, name):
    return bool(re.search(r'^\s*--%s(\s|$)' % re.escape(name), text, re.M))


def mpv_commands(ipc_path, commands, timeout=2.5):
    """Run commands over one mpv JSON-IPC connection.

    Returns a list of reply dicts, one per command (None if no reply).
    """
    replies = [None] * len(commands)

    def worker():
        conn = None
        try:
            if os.name == 'nt':
                conn = open(ipc_path, 'r+b', buffering=0)
                send, reader = conn.write, conn
            else:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.settimeout(1.5)
                conn.connect(ipc_path)
                send, reader = conn.sendall, conn.makefile('rb')
            for i, command in enumerate(commands):
                send((json.dumps({'command': command, 'request_id': i + 1}) + '\n').encode('utf-8'))
            pending = len(commands)
            while pending:
                line = reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode('utf-8'))
                except ValueError:
                    continue
                rid = msg.get('request_id')
                if isinstance(rid, int) and 1 <= rid <= len(commands) and replies[rid - 1] is None:
                    replies[rid - 1] = msg
                    pending -= 1
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    return replies


def install_keymap():
    folder = xbmcvfs.translatePath('special://userdata/keymaps/')
    path = os.path.join(folder, 'service.pip.xml')
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as fh:
                if fh.read() == KEYMAP_XML:
                    return
        os.makedirs(folder, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(KEYMAP_XML)
        xbmc.executebuiltin('Action(reloadkeymaps)')
        log('Installed keymap %s' % path)
    except OSError as exc:
        log('Could not write keymap: %s' % exc, xbmc.LOGERROR)


def write_input_conf():
    folder = xbmcvfs.translatePath('special://profile/addon_data/%s/' % ADDON_ID)
    path = os.path.join(folder, 'mpv-input.conf')
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(INPUT_CONF)
        return path
    except OSError as exc:
        log('Could not write input.conf: %s' % exc, xbmc.LOGWARNING)
        return None


# Service

class PiPService(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()
        self.proc = None
        self.ipc_path = None
        self.current_file = None      # Kodi's raw playing-file string loaded in PiP
        self.last_pos = None
        self.live = False
        self.mode = MODE_MUTE
        self.resume = True
        self.grace = 12
        self.paused_by_us = False
        self.muted_by_us = False
        self.pending_close = None     # deadline (time.time()) for closing an idle PiP
        self.pin_supported = False
        self.pinned = False
        self._opts = {}

    #Kodi events
    def onNotification(self, sender, method, data):
        if sender == ADDON_ID and method == 'Other.toggle':
            threading.Thread(target=self.toggle, daemon=True).start()
        elif sender == 'xbmc' and method == 'Player.OnStop':
            try:
                ended = bool(json.loads(data).get('end'))
            except (ValueError, AttributeError):
                ended = False
            threading.Thread(target=self.on_kodi_stop, args=(ended,), daemon=True).start()
        elif sender == 'xbmc' and method == 'Player.OnAVStart':
            threading.Thread(target=self.on_kodi_av_start, daemon=True).start()

    def is_open(self):
        return self.proc is not None and self.proc.poll() is None

    def toggle(self):
        with self.lock:
            if self.is_open():
                self.close_pip()
            else:
                self.open_pip()

    # Kodi item ended / next item started
    def on_kodi_stop(self, ended):
        with self.lock:
            if not self.is_open():
                return
            # Wait for auto-play to start the next item; close if none arrives.
            self.pending_close = time.time() + self.grace
            if not ended:
                mpv_commands(self.ipc_path, [['stop']])   # blank the window at once

    def on_kodi_av_start(self):
        with self.lock:
            if not self.is_open():
                return
            player = xbmc.Player()
            if not player.isPlayingVideo():
                return
            try:
                raw = player.getPlayingFile()
                pos = player.getTime()
                total = player.getTotalTime()
            except RuntimeError:
                return
            if raw == self.current_file:
                self.pending_close = None
                return
            source, headers, error = resolve_source(raw)
            if error:
                notify(error)
                self.pending_close = time.time() + self.grace
                return
            self.pending_close = None
            self.current_file = raw
            self.live = total <= 0
            self.last_pos = None if self.live else pos
            self._apply_kodi_mode(player)
            ua, referrer, fields = split_headers(headers)
            start = '%.2f' % pos if (not self.live and pos > 1) else 'none'
            replies = mpv_commands(self.ipc_path, [
                ['set_property', 'user-agent', ua],
                ['set_property', 'referrer', referrer],
                ['set_property', 'http-header-fields', fields],
                ['set_property', 'start', start],
                ['loadfile', source, 'replace'],
            ])
            log('Loaded next item into PiP: %s' % [r and r.get('error') for r in replies])

    def _apply_kodi_mode(self, player):
        if self.mode == MODE_PAUSE:
            if not xbmc.getCondVisibility('Player.Paused'):
                player.pause()
            self.paused_by_us = True
        elif not self.muted_by_us and not kodi_muted():
            set_kodi_mute(True)
            self.muted_by_us = True

    # open
    def _options_for(self, mpv):
        if mpv not in self._opts:
            self._opts[mpv] = mpv_option_text(mpv)
        return self._opts[mpv]

    def open_pip(self):
        player = xbmc.Player()
        if not player.isPlayingVideo():
            return
        addon = xbmcaddon.Addon(ADDON_ID)

        mpv = find_mpv(addon)
        if not mpv:
            notify('mpv not found. Install mpv or set its path in the add-on settings.')
            return

        try:
            raw = player.getPlayingFile()
            pos = player.getTime()
            total = player.getTotalTime()
        except RuntimeError:
            return

        source, headers, error = resolve_source(raw)
        if error:
            notify(error)
            return

        self.live = total <= 0
        self.mode = addon.getSettingInt('kodi_mode')
        self.resume = addon.getSettingBool('resume_kodi')
        self.grace = max(3, addon.getSettingInt('grace'))
        size = max(10, min(60, addon.getSettingInt('size')))
        corner = CORNERS[max(0, min(len(CORNERS) - 1, addon.getSettingInt('corner')))]

        opts = self._options_for(mpv)
        self.pin_supported = has_option(opts, 'auto-window-resize')
        self.pinned = False

        if os.name == 'nt':
            self.ipc_path = r'\\.\pipe\kodi-pip-%d' % os.getpid()
        else:
            self.ipc_path = os.path.join(tempfile.gettempdir(), 'kodi-pip-%d.sock' % os.getpid())
            try:
                os.remove(self.ipc_path)
            except OSError:
                pass

        cmd = [
            mpv, '--no-terminal', '--keep-open=no',
            # Stay open (blank) between items so the window keeps its place.
            '--idle=yes', '--force-window=yes',
            '--title=Kodi PiP', '--hwdec=auto-safe',
            '--ontop', '--no-border', '--osc=no',
            '--autofit=%d%%x%d%%' % (size, size),
            '--geometry=' + corner,
            '--input-ipc-server=' + self.ipc_path,
        ]
        if has_option(opts, 'focus-on'):
            cmd.append('--focus-on=never')
        elif has_option(opts, 'focus-on-open'):
            cmd.append('--focus-on-open=no')
        conf = write_input_conf()
        if conf:
            cmd.append('--input-conf=' + conf)
        if not self.live and pos > 1:
            cmd.append('--start=%.2f' % pos)
        cmd += header_args(headers)
        extra = addon.getSettingString('extra_args').strip()
        if extra:
            cmd += shlex.split(extra, posix=(os.name != 'nt'))
        cmd += ['--', source]

        log('Launching: %s' % ' '.join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **popen_extras())
        except OSError as exc:
            notify('Could not start mpv: %s' % exc)
            self.proc = None
            return

        self.current_file = raw
        self.last_pos = None if self.live else pos
        self.pending_close = None
        self.paused_by_us = False
        self.muted_by_us = False
        self._apply_kodi_mode(player)

    # close
    def close_pip(self):
        self._poll()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._after_close()

    def _after_close(self):
        self.proc = None
        self.pending_close = None
        if self.ipc_path and os.name != 'nt':
            try:
                os.remove(self.ipc_path)
            except OSError:
                pass

        if self.muted_by_us:
            set_kodi_mute(False)
            self.muted_by_us = False

        player = xbmc.Player()
        if self.resume and player.isPlayingVideo():
            try:
                same_item = player.getPlayingFile() == self.current_file
                if same_item and self.last_pos is not None and not self.live:
                    if self.paused_by_us or abs(player.getTime() - self.last_pos) > 5:
                        player.seekTime(self.last_pos)
                if self.paused_by_us and xbmc.getCondVisibility('Player.Paused'):
                    player.pause()
            except RuntimeError:
                pass
        self.paused_by_us = False

    def _poll(self):
        """Refresh last_pos; return True if mpv is idle (nothing loaded)."""
        if not self.ipc_path:
            return False
        pos_reply, idle_reply = mpv_commands(
            self.ipc_path,
            [['get_property', 'time-pos'], ['get_property', 'idle-active']])
        if pos_reply and isinstance(pos_reply.get('data'), (int, float)) and not self.live:
            self.last_pos = float(pos_reply['data'])
            if self.pin_supported and not self.pinned:
                # After the first file has opened, stop mpv re-fitting the window
                # to each new video so a manual move/resize survives episodes.
                mpv_commands(self.ipc_path, [['set_property', 'auto-window-resize', 'no']])
                self.pinned = True
        return bool(idle_reply and idle_reply.get('data') is True)

    #  service loop 
    
    def tick(self):
        with self.lock:
            if self.proc is None:
                return
            if self.proc.poll() is not None:
                # PiP window was closed by the user (middle-click / window manager).
                self._after_close()
                return
            if self.pending_close and time.time() >= self.pending_close:
                log('No next item started; closing PiP')
                self.close_pip()
                return
            idle = self._poll()
            if idle and self.mode == MODE_PAUSE and not self.pending_close:
                # Kodi is paused so it cannot auto-play; the PiP stream is over.
                self.close_pip()

    def shutdown(self):
        with self.lock:
            if self.is_open():
                self.proc.terminate()
            self.proc = None
            if self.muted_by_us:
                set_kodi_mute(False)
                self.muted_by_us = False


def main():
    install_keymap()
    service = PiPService()
    while not service.waitForAbort(1):
        service.tick()
    service.shutdown()


if __name__ == '__main__':
    main()
