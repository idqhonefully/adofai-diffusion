#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inspect a MIDI file: tracks, channels, note counts, program changes.
No third-party deps; manual parse of the MIDI binary format.
"""
import sys, struct

GM = {
 0:"Acoustic Grand Piano",1:"Bright Acoustic Piano",2:"Electric Grand Piano",3:"Honky-tonk Piano",
 4:"Electric Piano 1",5:"Electric Piano 2",6:"Harpsichord",7:"Clavi",
 8:"Celesta",9:"Glockenspiel",10:"Music Box",11:"Vibraphone",12:"Marimba",13:"Xylophone",14:"Tubular Bells",15:"Dulcimer",
 16:"Drawbar Organ",17:"Percussive Organ",18:"Rock Organ",19:"Church Organ",20:"Reed Organ",21:"Accordion",22:"Harmonica",23:"Tango Accordion",
 24:"Acoustic Guitar (nylon)",25:"Acoustic Guitar (steel)",26:"Electric Guitar (jazz)",27:"Electric Guitar (clean)",28:"Electric Guitar (muted)",29:"Overdriven Guitar",30:"Distortion Guitar",31:"Guitar harmonics",
 32:"Acoustic Bass",33:"Electric Bass (finger)",34:"Electric Bass (pick)",35:"Fretless Bass",36:"Slap Bass 1",37:"Slap Bass 2",38:"Synth Bass 1",39:"Synth Bass 2",
 40:"Violin",41:"Viola",42:"Cello",43:"Contrabass",44:"Tremolo Strings",45:"Pizzicato Strings",46:"Orchestral Harp",47:"Timpani",
 48:"String Ensemble 1",49:"String Ensemble 2",50:"SynthStrings 1",51:"SynthStrings 2",52:"Choir Aahs",53:"Voice Oohs",54:"Synth Voice",55:"Orchestra Hit",
 56:"Trumpet",57:"Trombone",58:"Tuba",59:"Muted Trumpet",60:"French Horn",61:"Brass Section",62:"SynthBrass 1",63:"SynthBrass 2",
 64:"Soprano Sax",65:"Alto Sax",66:"Tenor Sax",67:"Baritone Sax",68:"Oboe",69:"English Horn",70:"Bassoon",71:"Clarinet",
 72:"Piccolo",73:"Flute",74:"Recorder",75:"Pan Flute",76:"Blown Bottle",77:"Shakuhachi",78:"Whistle",79:"Ocarina",
 80:"Lead 1 (square)",81:"Lead 2 (sawtooth)",82:"Lead 3 (calliope)",83:"Lead 4 (chiff)",84:"Lead 5 (charang)",85:"Lead 6 (voice)",86:"Lead 7 (fifths)",87:"Lead 8 (bass+lead)",
 88:"Pad 1 (new age)",89:"Pad 2 (warm)",90:"Pad 3 (polysynth)",91:"Pad 4 (choir)",92:"Pad 5 (bowed)",93:"Pad 6 (metallic)",94:"Pad 7 (halo)",95:"Pad 8 (sweep)",
 96:"FX 1 (rain)",97:"FX 2 (soundtrack)",98:"FX 3 (crystal)",99:"FX 4 (atmosphere)",100:"FX 5 (brightness)",101:"FX 6 (goblins)",102:"FX 7 (echoes)",103:"FX 8 (sci-fi)",
 104:"Sitar",105:"Banjo",106:"Shamisen",107:"Koto",108:"Kalimba",109:"Bag pipe",110:"Fiddle",111:"Shanai",
 112:"Tinkle Bell",113:"Agogo",114:"Steel Drums",115:"Woodblock",116:"Taiko Drum",117:"Melodic Tom",118:"Synth Drum",119:"Reverse Cymbal",
 120:"Guitar Fret Noise",121:"Breath Noise",122:"Seashore",123:"Bird Tweet",124:"Telephone Ring",125:"Helicopter",126:"Applause",127:"Gunshot",
}

def read_vlq(f):
    val = 0
    while True:
        b = f.read(1)[0]
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return val

def parse(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"MThd":
        print("Not a MIDI file"); return
    fmt, ntracks, division = struct.unpack(">HHH", data[8:14])
    print(f"Format={fmt}  Tracks={ntracks}  Division( ticks per quarter)={division}")
    off = 14
    for ti in range(ntracks):
        assert data[off:off+4] == b"MTrk", data[off:off+4]
        tlen = struct.unpack(">I", data[off+4:off+8])[0]
        track = data[off+8:off+8+tlen]
        off += 8 + tlen
        # parse events
        running = None
        i = 0
        track_name = None
        inst_name = None
        channels = {}  # ch -> {notes, program, name}
        while i < len(track):
            dt = 0
            while track[i] & 0x80:
                dt = (dt << 7) | (track[i] & 0x7F); i += 1
            status = track[i]
            if status & 0x80:
                i += 1
                # running status only valid for channel messages (0x80-0xEF)
                running = status if status < 0xF0 else None
            else:
                status = running
            if status is None:
                i += 1
                continue
            if status == 0xFF:
                meta = track[i]; i += 1
                mlen = track[i]; i += 1
                mdata = track[i:i+mlen]; i += mlen
                if meta == 0x03:
                    track_name = mdata.decode("latin1", "replace")
                elif meta == 0x04:
                    inst_name = mdata.decode("latin1", "replace")
                continue
            if status in (0xF0, 0xF7):
                while i < len(track) and track[i] != 0xF7:
                    i += 1
                i += 1
                continue
            c = status & 0x0F
            t = status & 0xF0
            if t == 0xC0:  # program change
                prog = track[i]; i += 1
                ch = channels.setdefault(c, {"notes":0,"program":None,"name":None})
                ch["program"] = prog
            elif t in (0x80, 0x90, 0xA0, 0xB0, 0xE0):  # 3-byte
                d1 = track[i]; d2 = track[i+1]; i += 2
                if t == 0x90 and d2 > 0:
                    ch = channels.setdefault(c, {"notes":0,"program":None,"name":None})
                    ch["notes"] += 1
            elif t in (0xD0,):  # channel pressure (2-byte)
                i += 1
            else:
                print(f"  [warn] unknown status {status:#x} at {i}")
                break
        print(f"\n--- Track {ti} ---  name={track_name!r} inst={inst_name!r}")
        if not channels:
            print("  (no channel messages / no notes)")
        for c, info in sorted(channels.items()):
            prog = info["program"]
            gname = GM.get(prog, f"prog {prog}") if prog is not None else "(no program change -> default piano)"
            print(f"  ch{c}: notes={info['notes']:>5}  program={prog}  -> {gname}")

if __name__ == "__main__":
    parse(sys.argv[1])
