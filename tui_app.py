import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.styles import Style
from prompt_toolkit.filters import has_focus
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document

# Reuse yewtube internals without modifying them
from mps_youtube import config, g, screen
from mps_youtube.playlist import Video
from mps_youtube import pafy

# UI style similar to original
style = Style.from_dict({
    'title': 'ansiyellow bold',
    'pane-border': 'ansiblue',
    'selected': 'reverse',
    'status': '#ffffff',
    'prompt': 'ansigreen',
})


@dataclass
class ChannelItem:
    id: str
    title: str
    description: str = ''


@dataclass
class VideoItem:
    id: str
    title: str
    length_seconds: int = 0


@dataclass
class AppState:
    query: str = ''
    channels: List[ChannelItem] = field(default_factory=list)
    selected_channel: int = 0
    videos: List[VideoItem] = field(default_factory=list)
    selected_video: int = 0
    now_playing: Optional[Tuple[str, int]] = None
    status_text: str = ''
    focused_pane: str = 'left'  # left/right


state = AppState()


# Hook yewtube status into footer
_original_writestatus = screen.writestatus

def _patched_writestatus(text: str, mute: bool = False):
    state.status_text = text
    if app is not None:
        app.invalidate()

screen.writestatus = _patched_writestatus


# UI components
def _handle_search_accept(buf: Buffer):
    text = buf.text.strip()
    if not text:
        return
    # Show searching status immediately
    state.status_text = 'Searching...'
    if app is not None:
        app.invalidate()
    try:
        results = pafy.channel_search(text)
        channels: List[ChannelItem] = []
        for r in results:
            if r.get('type') != 'channel':
                continue
            channels.append(ChannelItem(id=r.get('id'), title=r.get('title'), description=(r.get('descriptionSnippet') or [{'text': ''}])[0]['text'] if r.get('descriptionSnippet') else ''))
        state.channels = channels
        state.selected_channel = 0
        _load_videos_for_selected()
        state.status_text = f"Found {len(channels)} channel(s)."
    except Exception as e:
        state.status_text = f"Search failed: {e}"
    finally:
        # Clear buffer after handling
        buf.document = Document(text='')
        if app is not None:
            app.invalidate()

search_input = TextArea(
    height=1,
    prompt='Search channels: ',
    style='class:prompt',
    multiline=False,
    wrap_lines=False,
    accept_handler=_handle_search_accept,
)


def _fmt_duration_to_seconds(dur: Optional[str]) -> int:
    if not dur:
        return 0
    parts = [int(x) for x in dur.split(':') if x.isdigit()]
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    return 0


def render_channels():
    out = [('class:title', 'Channels\n')]
    if not state.channels:
        out.append(('', ' No channels. Type a query and press Enter.'))
        return out
    for idx, ch in enumerate(state.channels):
        prefix = '> ' if idx == state.selected_channel and state.focused_pane == 'left' else '  '
        style_key = 'class:selected' if idx == state.selected_channel and state.focused_pane == 'left' else ''
        out.append((style_key, f"{prefix}{ch.title}\n"))
    return out


def render_videos():
    out = [('class:title', 'Videos\n')]
    if not state.videos:
        out.append(('', ' No videos.'))
        return out
    for idx, v in enumerate(state.videos):
        dur = time.strftime('%H:%M:%S', time.gmtime(v.length_seconds)) if v.length_seconds >= 3600 else time.strftime('%M:%S', time.gmtime(v.length_seconds))
        prefix = '> ' if idx == state.selected_video and state.focused_pane == 'right' else '  '
        style_key = 'class:selected' if idx == state.selected_video and state.focused_pane == 'right' else ''
        out.append((style_key, f"{prefix}{v.title}  [{dur}]\n"))
    return out


channels_window = Window(content=FormattedTextControl(render_channels), wrap_lines=False)
videos_window = Window(content=FormattedTextControl(render_videos), wrap_lines=False)


# Footer / status bar

def _compute_footer() -> str:
    if state.now_playing:
        title, _ = state.now_playing
        return f"Playing: {title}  |  Controls: Enter=play audio, Shift+Enter=play video, Tab=switch pane, Esc=quit"
    return state.status_text or 'Tab: switch pane | Up/Down: navigate | Enter: search/select | Shift+Enter: play video | Esc: quit'

statusbar_window = Window(height=1, content=FormattedTextControl(lambda: [('class:status', _compute_footer())]))


root_container = HSplit([
    search_input,
    VSplit([
        channels_window,
        videos_window,
    ], padding=2),
    statusbar_window,
])

kb = KeyBindings()


@kb.add('enter', filter=~has_focus(search_input))
def _(event):
    # Enter outside the search box plays audio of selected video
    if state.focused_pane == 'right' and state.videos:
        _play_selected('audio')


@kb.add('v')
def _(event):
    # 'v' key: play video
    if not has_focus(search_input)() and state.focused_pane == 'right' and state.videos:
        _play_selected('video')


@kb.add('tab')
def _(event):
    if has_focus(search_input)():
        event.app.layout.focus(channels_window)
        state.focused_pane = 'left'
    else:
        cur = event.app.layout.current_window
        if cur is channels_window:
            event.app.layout.focus(videos_window)
            state.focused_pane = 'right'
        else:
            event.app.layout.focus(search_input)
    app.invalidate()


@kb.add('up')
def _(event):
    if has_focus(search_input)():
        return
    if state.focused_pane == 'left' and state.channels:
        state.selected_channel = max(0, state.selected_channel - 1)
        _load_videos_for_selected()
    elif state.focused_pane == 'right' and state.videos:
        state.selected_video = max(0, state.selected_video - 1)
    app.invalidate()


@kb.add('down')
def _(event):
    if has_focus(search_input)():
        return
    if state.focused_pane == 'left' and state.channels:
        state.selected_channel = min(len(state.channels) - 1, state.selected_channel + 1)
        _load_videos_for_selected()
    elif state.focused_pane == 'right' and state.videos:
        state.selected_video = min(len(state.videos) - 1, state.selected_video + 1)
    app.invalidate()


@kb.add('escape')
@kb.add('c-c')
def _(event):
    # Exit cleanly without raising KeyboardInterrupt
    event.app.exit()


# Playback helpers
import threading

def _play_selected(mode: str):
    if not state.videos:
        return
    vid = state.videos[state.selected_video]

    # Respect existing config; just toggle show_video based on mode
    try:
        config.SHOW_VIDEO.set(True if mode == 'video' else False)
    except Exception:
        pass

    yv = Video(vid.id, vid.title, vid.length_seconds)

    def _run():
        state.now_playing = (vid.title, vid.length_seconds)
        state.status_text = ''
        app.invalidate()
        try:
            # Use the project’s standard playback path
            from mps_youtube.player import BasePlayer, CmdPlayer, stream_details
            # Assign player based on config
            from mps_youtube.util import assign_player
            from mps_youtube import util
            util.assign_player(config.PLAYER.get)
            g.PLAYER_OBJ.play([yv], shuffle=False, repeat=False)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            state.status_text = f"Playback error: {e}"
        finally:
            state.now_playing = None
            app.invalidate()

    # Run playback in a background thread so the UI event loop stays alive
    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _load_videos_for_selected():
    if not state.channels:
        state.videos = []
        return
    ch = state.channels[state.selected_channel]
    try:
        vids = pafy.all_videos_from_channel(ch.id)
        items: List[VideoItem] = []
        for v in vids:
            vid = v.get('id') or (v.get('link').split('=')[-1] if v.get('link') else None)
            title = v.get('title')
            dur_str = v.get('duration') or ''
            dsec = _fmt_duration_to_seconds(dur_str)
            if vid and title:
                items.append(VideoItem(id=vid, title=title, length_seconds=dsec))
        state.videos = items
        state.selected_video = 0
    except Exception as e:
        state.status_text = f"Failed to load videos: {e}"
        state.videos = []


app: Optional[Application] = None

def run():
    global app
    app = Application(
        layout=Layout(root_container),
        key_bindings=kb,
        style=style,
        full_screen=True,
    )
    search_input.buffer = Buffer(document=Document(text=''))
    app.layout.focus(search_input)
    try:
        app.run()
    except KeyboardInterrupt:
        # Swallow Ctrl-C to avoid traceback on exit
        pass


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        pass
