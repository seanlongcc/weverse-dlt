#!/usr/bin/env python3
# weverse_chat_to_ass_twitch.py

import argparse
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def ass_time(t: float) -> str:
    # ASS uses h:mm:ss.cc (centiseconds)
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    t -= h * 3600
    m = int(t // 60)
    t -= m * 60
    s = int(t)
    cs = int(round((t - s) * 100))
    if cs == 100:
        s += 1
        cs = 0
    if s == 60:
        m += 1
        s = 0
    if m == 60:
        h += 1
        m = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    # Escape ASS override braces and backslashes; normalize newlines.
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{").replace("}", r"\}")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")
    return text


NAME_COLOR_ASS = "&H00B0B0B0&"
MSG_COLOR_ASS = "&H00FFFFFF&"

def render_chat_text(name: str, msg: str) -> str:
    name_esc = ass_escape(name)
    msg_esc = ass_escape(msg)
    if name_esc:
        if msg_esc:
            return f"{{\\1c{NAME_COLOR_ASS}}}{name_esc}{{\\1c{MSG_COLOR_ASS}}}: {msg_esc}"
        return f"{{\\1c{NAME_COLOR_ASS}}}{name_esc}{{\\1c{MSG_COLOR_ASS}}}"
    return msg_esc


def pick_fields(item: Dict[str, Any]) -> Tuple[Optional[int], str, str]:
    # Expected Weverse paginator objects:
    # messageTime (ms), profile.profileName, content
    ts = item.get("messageTime") or item.get("createTime") or item.get("updateTime")
    name = ""
    prof = item.get("profile") or {}
    if isinstance(prof, dict):
        name = (prof.get("profileName") or "").strip()
    if not name:
        name = (item.get("name") or "").strip()

    msg = (item.get("content") or item.get("message") or "").strip()
    return (int(ts) if ts is not None else None), name, msg


@dataclass
class Segment:
    start: float
    end: float
    slot: int
    move_from_slot: Optional[int] = None
    final: bool = False


@dataclass
class ChatMsg:
    idx: int
    start: float
    expire: float
    name: str
    msg: str
    cur_start: Optional[float] = None
    cur_slot: Optional[int] = None
    cur_move_from: Optional[int] = None
    segments: List[Segment] = field(default_factory=list)

    def start_segment(self, t: float, slot: int, move_from_slot: Optional[int]) -> None:
        self.cur_start = t
        self.cur_slot = slot
        self.cur_move_from = move_from_slot

    def close_segment(self, t: float) -> None:
        if self.cur_start is None or self.cur_slot is None:
            return
        # Ignore zero/negative length
        if t <= self.cur_start + 1e-6:
            self.cur_start = t
            return
        self.segments.append(
            Segment(
                start=self.cur_start,
                end=t,
                slot=self.cur_slot,
                move_from_slot=self.cur_move_from,
                final=False,
            )
        )
        self.cur_start = t
    def end(self, t: float) -> None:
        """Finalize this message: close current segment and prevent any further segments."""
        if self.cur_start is None or self.cur_slot is None:
            # already ended / never started
            self.cur_start = None
            self.cur_slot = None
            self.cur_move_from = None
            return
        # Close any visible time (allow zero length, but then just clear)
        if t > self.cur_start + 1e-6:
            self.segments.append(
                Segment(
                    start=self.cur_start,
                    end=t,
                    slot=self.cur_slot,
                    move_from_slot=self.cur_move_from,
                    final=False,
                )
            )
        # Clear state so we don't accidentally extend later (e.g., via cleanup)
        self.cur_start = None
        self.cur_slot = None
        self.cur_move_from = None




def build_twitch_segments(
    msgs_in: List[Tuple[float, str, str]],
    hold: float,
    max_lines: int,
) -> List[ChatMsg]:
    # msgs_in: list of (time_seconds, name, message)
    # Event simulation:
    # - arrival pushes stack up; if full, top message is dropped
    # - expiry removes message and stack shifts down to fill
    messages: List[ChatMsg] = []
    events: List[Tuple[float, int, str]] = []  # (time, idx, kind 'exp'/'arr')

    for i, (t, name, msg) in enumerate(msgs_in):
        cm = ChatMsg(
            idx=i,
            start=t,
            expire=t + hold,
            name=name,
            msg=msg,
        )
        messages.append(cm)
        events.append((t, i, "arr"))
        events.append((t + hold, i, "exp"))

    # Sort: time, then expiries before arrivals at same time
    def ev_key(e: Tuple[float, int, str]) -> Tuple[float, int]:
        t, _, kind = e
        return (t, 0 if kind == "exp" else 1)

    events.sort(key=ev_key)

    active: List[int] = []  # message indices, bottom-to-top (slot 0 is bottom)

    for t, idx, kind in events:
        if kind == "arr":
            # If already expired before arrival time (shouldn't happen), skip
            cm_new = messages[idx]
            if cm_new.expire <= t:
                continue

            # If stack full, drop the topmost first
            if len(active) >= max_lines:
                top_idx = active[-1]
                top = messages[top_idx]
                top.end(t)
                # stop tracking it
                active.pop()

            # Shift everyone up by 1 slot (close & restart)
            for slot, midx in enumerate(active):
                cm = messages[midx]
                cm.close_segment(t)
                cm.start_segment(t, slot + 1, move_from_slot=slot)

            # Insert new at bottom
            cm_new.start_segment(t, 0, move_from_slot=None)
            active.insert(0, idx)

        else:  # expiry
            if idx not in active:
                continue
            # Remove expired message
            pos = active.index(idx)
            cm = messages[idx]
            cm.end(t)
            active.pop(pos)

            # Shift down messages above it to fill gap
            for j in range(pos, len(active)):
                midx = active[j]
                cm2 = messages[midx]
                old_slot = j + 1
                new_slot = j
                cm2.close_segment(t)
                cm2.start_segment(t, new_slot, move_from_slot=old_slot)

    # Close any remaining active segments (usually none, since every message has an expiry event)
    for midx in list(active):
        messages[midx].end(messages[midx].expire)


    # Mark final segment per message
    for cm in messages:
        if cm.segments:
            cm.segments[-1].final = True

    return messages


def make_ass(
    chat_msgs: List[ChatMsg],
    resx: int,
    resy: int,
    margin_l: int,
    margin_r: int,
    margin_v: int,
    font_name: str,
    font_size: int,
    outline: int,
    shadow: int,
    line_gap: int,
    shift: float,
    fade_out: float,
) -> str:
    # Approx line height; good enough to prevent overlap
    line_h = font_size + line_gap + outline * 2

    header = (
        "[Script Info]\n"
        "; Script generated by weverse_chat_to_ass_twitch.py\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {resx}\n"
        f"PlayResY: {resy}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
        " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Chat,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{outline},{shadow},1,{margin_l},{margin_r},{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    shift_ms = int(round(shift * 1000))
    fade_ms = int(round(fade_out * 1000))

    lines: List[str] = [header]

    x = margin_l

    def y_for_slot(slot: int) -> int:
        return int(resy - margin_v - slot * line_h)

    for cm in chat_msgs:
        for seg in cm.segments:
            start = seg.start
            end = seg.end
            # Ensure minimally positive duration
            if end <= start + 0.01:
                end = start + 0.01

            y1 = y_for_slot(seg.slot)

            tags = ["\\an1"]  # bottom-left

            if seg.move_from_slot is not None and shift_ms > 0:
                y0 = y_for_slot(seg.move_from_slot)
                tags.append(f"\\move({x},{y0},{x},{y1},0,{shift_ms})")
            else:
                tags.append(f"\\pos({x},{y1})")

            # Fade only on final disappearance
            if seg.final and fade_ms > 0:
                tags.append(f"\\fad(0,{fade_ms})")

            tagblock = "{" + "".join(tags) + "}"
            text = render_chat_text(cm.name, cm.msg)

            lines.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Chat,,{margin_l},{margin_r},{margin_v},,{tagblock}{text}\n"
            )

    return "".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required=True, help="Input chat JSON (Weverse paginator output)")
    ap.add_argument("--ass", required=True, help="Output .ass path")
    ap.add_argument("--max-lines", type=int, default=6, help="Max lines visible (Twitch-style stack)")
    ap.add_argument("--hold", type=float, default=10.0, help="Seconds each message lives (unless pushed out)")
    ap.add_argument("--shift", type=float, default=0.0, help="Seconds to animate stack movement (0 = no animation)")
    ap.add_argument("--fade-out", type=float, default=0.0, help="Fade-out seconds when a message disappears")
    ap.add_argument("--offset-seconds", type=float, default=0.0, help="Manual sync offset (+ delays chat, - advances chat)")
    ap.add_argument("--resx", type=int, default=1080)
    ap.add_argument("--resy", type=int, default=1920)
    ap.add_argument("--margin-l", type=int, default=10)
    ap.add_argument("--margin-r", type=int, default=10)
    ap.add_argument("--margin-v", type=int, default=10)
    ap.add_argument("--font-name", default="Nanum Gothic")
    ap.add_argument("--font-size", type=int, default=36)
    ap.add_argument("--outline", type=int, default=2)
    ap.add_argument("--shadow", type=int, default=0)
    ap.add_argument("--line-gap", type=int, default=2)

    args = ap.parse_args()

    with open(args.chat, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise SystemExit("Chat JSON must be a list of messages.")

    parsed: List[Tuple[int, str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ts, name, msg = pick_fields(item)
        if not msg and not name:
            continue
        parsed.append((ts if ts is not None else -1, name, msg))

    # If we have timestamps, sort and zero them
    have_ts = any(ts >= 0 for ts, _, _ in parsed)
    if have_ts:
        parsed = [p for p in parsed if p[0] >= 0]
        parsed.sort(key=lambda x: x[0])
        base = parsed[0][0]
        msgs_in: List[Tuple[float, str, str]] = []
        for ts, name, msg in parsed:
            t = (ts - base) / 1000.0 + args.offset_seconds
            if t < 0:
                t = 0.0
            msgs_in.append((t, name, msg))
    else:
        # Fallback: no timestamps; space them out 1s apart
        msgs_in = []
        for i, (_, name, msg) in enumerate(parsed):
            t = i * 1.0 + args.offset_seconds
            if t < 0:
                t = 0.0
            msgs_in.append((t, name, msg))

    chat_msgs = build_twitch_segments(
        msgs_in=msgs_in,
        hold=args.hold,
        max_lines=max(1, args.max_lines),
    )

    ass_text = make_ass(
        chat_msgs=chat_msgs,
        resx=args.resx,
        resy=args.resy,
        margin_l=args.margin_l,
        margin_r=args.margin_r,
        margin_v=args.margin_v,
        font_name=args.font_name,
        font_size=args.font_size,
        outline=args.outline,
        shadow=args.shadow,
        line_gap=args.line_gap,
        shift=max(0.0, args.shift),
        fade_out=max(0.0, args.fade_out),
    )

    with open(args.ass, "w", encoding="utf-8-sig", newline="") as f:
        f.write(ass_text)

    total_segments = sum(len(m.segments) for m in chat_msgs)
    print(f"Wrote: {args.ass} ({total_segments} dialogue segments)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
