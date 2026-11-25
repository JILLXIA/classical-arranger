"""Render the exact arrangement with the system's FluidSynth 2.x library."""
import ctypes as C
import ctypes.util
import os
from contextlib import ExitStack
from pathlib import Path


def render_preview(midi: Path, soundfont: Path, output: Path) -> None:
    libname = os.environ.get("FLUIDSYNTH_LIBRARY") or ctypes.util.find_library("fluidsynth")
    if not libname or not soundfont.is_file():
        raise ValueError("Install FluidSynth 2.x and set SOUNDFONT to a General MIDI .sf2 file.")
    lib = C.CDLL(libname)
    p, s, i, d = C.c_void_p, C.c_char_p, C.c_int, C.c_double

    def bind(name, result, *args):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = list(args), result
        return fn

    new_settings = bind("new_fluid_settings", p)
    setstr = bind("fluid_settings_setstr", i, p, s, s)
    setnum = bind("fluid_settings_setnum", i, p, s, d)
    new_synth = bind("new_fluid_synth", p, p)
    sfload = bind("fluid_synth_sfload", i, p, s, i)
    new_player = bind("new_fluid_player", p, p)
    add = bind("fluid_player_add", i, p, s)
    play = bind("fluid_player_play", i, p)
    status = bind("fluid_player_get_status", i, p)
    new_renderer = bind("new_fluid_file_renderer", p, p)
    process = bind("fluid_file_renderer_process_block", i, p)
    with ExitStack() as resources:
        def own(obj, destructor):
            if not obj:
                raise RuntimeError("FluidSynth could not allocate " + destructor.removeprefix("delete_fluid_"))
            resources.callback(bind(destructor, None, p), obj)
            return obj

        settings = own(new_settings(), "delete_fluid_settings")
        for key, value in [(b"audio.file.name", os.fsencode(output)),
                           (b"audio.file.type", b"wav"), (b"player.timing-source", b"sample")]:
            if setstr(settings, key, value) < 0:
                raise RuntimeError("FluidSynth setting unavailable: " + key.decode())
        if setnum(settings, b"synth.sample-rate", 48000.0) < 0:
            raise RuntimeError("Could not configure sample rate.")
        synth = own(new_synth(settings), "delete_fluid_synth")
        if sfload(synth, os.fsencode(soundfont), 1) < 0:
            raise ValueError("Could not load the specified SoundFont.")
        player = own(new_player(synth), "delete_fluid_player")
        if add(player, os.fsencode(midi)) < 0:
            raise ValueError("Could not load MIDI into FluidSynth.")
        renderer = own(new_renderer(synth), "delete_fluid_file_renderer")
        if play(player) < 0:
            raise RuntimeError("Could not start MIDI playback.")
        while status(player) == 1:  # FLUID_PLAYER_PLAYING
            if process(renderer) < 0:
                raise RuntimeError("Failed to write the preview WAV.")
    if not output.is_file() or output.stat().st_size < 45:
        raise RuntimeError("FluidSynth produced an empty preview.")
