#!/usr/bin/env python3
"""Download videos from the Česká televize iVysílání archive (ceskatelevize.cz).

Examples:
  ct-download.py https://www.ceskatelevize.cz/porady/10111104372-prasatko-peppa/ --list
  ct-download.py <show-url>                         # download all playable episodes
  ct-download.py <show-url> --select 1-5,9          # by number shown in --list
  ct-download.py <show-url> --season "III" --quality 720p
  ct-download.py <show-url> --interactive           # list, then ask what to download
  ct-download.py https://www.ceskatelevize.cz/porady/10111104372-prasatko-peppa/215384603670005/

Requires only Python 3.9+ and ffmpeg (brew install ffmpeg). Only non-DRM streams
are supported.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "Referer": "https://www.ceskatelevize.cz/",
}
GRAPHQL = "https://api.ceskatelevize.cz/graphql/"
STREAM_DATA = ("https://api.ceskatelevize.cz/video/v1/playlist-vod/v1/stream-data/"
               "media/external/{idec}?canPlayDrm=false&streamType=hls&quality=web")
# Quality labels CT uses, best first. The HLS master playlist doesn't carry
# resolutions, so variants (sorted by bandwidth) are matched to these labels.
QUALITY_ORDER = ["1080p", "720p", "576p", "404p", "288p", "144p1", "144p2"]

EPISODES_QUERY = """
query GetEpisodes($programmeId: String!, $seasonId: String, $limit: PaginationAmount!,
                  $after: String, $orderBy: EpisodeOrderByType!, $onlyPlayable: Boolean) {
  programme(programmeId: $programmeId) {
    episodes(seasonId: $seasonId, limit: $limit, after: $after, orderBy: $orderBy,
             onlyPlayable: $onlyPlayable) {
      totalCount
      nextCursor
      items { idec title duration playable showTitle season { title } }
    }
  }
}"""


@dataclass
class Episode:
    idec: str
    title: str
    season: str
    duration: int | None
    number: int = 0       # position within season (oldest first)
    index: int = 0        # global position, used by --select


# ---------------------------------------------------------------- HTTP helpers

def http_get(url: str, accept_json: bool = False) -> str:
    headers = dict(HEADERS)
    if accept_json:
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def graphql(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GRAPHQL, data=body, headers={
        **HEADERS, "Accept": "application/json", "Content-Type": "application/json",
        "Origin": "https://www.ceskatelevize.cz"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("errors"):
        raise RuntimeError(f"GraphQL error: {data['errors'][0].get('message')}")
    return data["data"]


# --------------------------------------------------------------- CT metadata

def parse_url(url: str) -> tuple[str, str | None]:
    """Return (show page URL, episode IDEC or None)."""
    m = re.search(r"ceskatelevize\.cz/(?:ivysilani/)?porady/([^/?#]+)/?(\d{11,15})?", url)
    if not m:
        raise SystemExit(f"Unrecognised URL: {url}")
    return f"https://www.ceskatelevize.cz/porady/{m.group(1)}/", m.group(2)


def load_programme(show_url: str) -> dict:
    html = http_get(show_url)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise SystemExit("Could not find page data (site layout changed?)")
    programme = json.loads(m.group(1))["props"]["pageProps"]["data"]["programme"]
    return programme


def fetch_episodes(programme_id: str, season_id: str | None, only_playable: bool) -> list[dict]:
    items, cursor = [], None
    while True:
        data = graphql(EPISODES_QUERY, {
            "programmeId": programme_id, "seasonId": season_id, "limit": 40,
            "after": cursor, "orderBy": "oldest", "onlyPlayable": only_playable})
        eps = data["programme"]["episodes"]
        items += eps["items"]
        cursor = eps.get("nextCursor")
        if not cursor or not eps["items"]:
            return items


def list_episodes(programme: dict, season_filter: str | None, only_playable: bool) -> list[Episode]:
    seasons = programme.get("seasons") or []
    if season_filter:
        wanted = [s for s in seasons if season_matches(s["title"], season_filter)]
        if not wanted:
            names = ", ".join(clean(s["title"]) for s in seasons) or "(none)"
            raise SystemExit(f"No season matches '{season_filter}'. Seasons: {names}")
    else:
        wanted = seasons

    episodes: list[Episode] = []
    if not wanted:  # show without seasons
        groups = [(None, fetch_episodes(programme["programmeId"], None, only_playable))]
    else:
        groups = [(s, fetch_episodes(programme["programmeId"], s["id"],
                                     only_playable)) for s in wanted]
    for season, items in groups:
        for n, it in enumerate(items, 1):
            episodes.append(Episode(
                idec=it["idec"], title=clean(it["title"]),
                season=clean(season["title"]) if season else clean((it.get("season") or {}).get("title") or ""),
                duration=it.get("duration"), number=n))
    for i, ep in enumerate(episodes, 1):
        ep.index = i
    return episodes


def season_matches(title: str, flt: str) -> bool:
    t, f = clean(title).lower(), flt.strip().lower()
    return t == f or t.split(".")[0] == f.rstrip(".") or (len(f) > 3 and f in t)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def stream_info(idec: str) -> dict:
    try:
        data = json.loads(http_get(STREAM_DATA.format(idec=idec), accept_json=True))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"stream-data request failed ({e.code}) – episode may be unavailable")
    if not data.get("streams"):
        raise RuntimeError("no streams (episode not playable or DRM only)")
    return data


# ---------------------------------------------------------------- HLS / ffmpeg

def pick_variant(master_url: str, quality: str) -> tuple[str, str | None, str]:
    """Return (video playlist URL, audio playlist URL or None, quality label)."""
    master = http_get(master_url)
    audio: dict[str, str] = {}
    variants: list[tuple[int, str, str | None]] = []
    lines = master.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            g = re.search(r'GROUP-ID="([^"]+)"', line)
            u = re.search(r'URI="([^"]+)"', line)
            if g and u:
                audio.setdefault(g.group(1), urllib.parse.urljoin(master_url, u.group(1)))
        elif line.startswith("#EXT-X-STREAM-INF:"):
            bw = int(re.search(r"BANDWIDTH=(\d+)", line).group(1))
            grp = re.search(r'AUDIO="([^"]+)"', line)
            url = next(l for l in lines[i + 1:] if l and not l.startswith("#"))
            variants.append((bw, urllib.parse.urljoin(master_url, url), grp.group(1) if grp else None))
    if not variants:  # already a media playlist
        return master_url, None, "?"
    variants.sort(key=lambda v: v[0], reverse=True)
    labels = QUALITY_ORDER[:len(variants)]
    if quality == "best":
        idx = 0
    elif quality == "worst":
        idx = len(variants) - 1
    elif quality in labels:
        idx = labels.index(quality)
    else:  # requested quality not offered: take the best one not above it
        req = QUALITY_ORDER.index(quality) if quality in QUALITY_ORDER else 0
        idx = next((i for i, l in enumerate(labels) if QUALITY_ORDER.index(l) >= req), len(variants) - 1)
    bw, vurl, grp = variants[idx]
    return vurl, audio.get(grp) if grp else None, labels[idx]


def ffmpeg_download(video: str, audio: str | None, out: Path, limit_seconds: int | None) -> None:
    hdr = f"User-Agent: {UA}\r\nReferer: https://www.ceskatelevize.cz/\r\n"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
           "-headers", hdr, "-i", video]
    if audio:
        cmd += ["-headers", hdr, "-i", audio, "-map", "0:v:0", "-map", "1:a:0"]
    if limit_seconds:
        cmd += ["-t", str(limit_seconds)]
    cmd += ["-c", "copy", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)


def download_subtitles(info: dict, base: Path) -> None:
    for sub in info["streams"][0].get("subtitles") or []:
        for f in sub.get("files", []):
            if f.get("format") == "vtt":
                path = base.with_name(f"{base.name}.{sub.get('language', 'und')}.vtt")
                path.write_text(http_get(f["url"]), encoding="utf-8")


def safe(name: str) -> str:
    """Filesystem-safe name keeping Czech characters (used with --keep-diacritics)."""
    return re.sub(r'[\\/:*?"<>|]+', "-", name).strip(" .")


def plain(name: str) -> str:
    """ASCII-only name: 'III. řada' -> 'III rada', 'Prasátko Peppa!' -> 'Prasatko Peppa'.

    Diacritics are stripped (ě->e, š->s, č->c, ř->r, ž->z, ů->u, ...), then
    anything that isn't a letter, digit, space or hyphen is removed.
    """
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9 -]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def file_name(parts: list[str], ascii_names: bool) -> str:
    """Clean each part separately and join them with ' - '."""
    fix = plain if ascii_names else safe
    return " - ".join(p for p in (fix(x) for x in parts) if p)


def download(ep: Episode, show_title: str, out_dir: Path, args) -> bool:
    info = stream_info(ep.idec)
    # episodeTitle looks like "Karavan | 5" – use the real episode number if present
    m = re.search(r"\|\s*(\d+)\s*$", clean(info.get("episodeTitle") or ""))
    number = int(m.group(1)) if m else ep.number
    title = ep.title or clean((info.get("episodeTitle") or "").split("|")[0])
    ascii_names = not args.keep_diacritics
    name = file_name([show_title, ep.season, f"{number:02d}" if number else "", title, ep.idec],
                     ascii_names)
    base = out_dir / file_name([show_title], ascii_names) / name
    target = base.with_name(base.name + ".mp4")
    if target.exists() and not args.overwrite:
        print(f"  skip (exists): {target}")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    video, audio, label = pick_variant(info["streams"][0]["url"], args.quality)
    print(f"  -> {target}  [{label}]")
    tmp = base.with_name(base.name + ".part.mp4")
    try:
        ffmpeg_download(video, audio, tmp, args.test_seconds)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    if args.subs:
        download_subtitles(info, base)
    return True


# ---------------------------------------------------------------------- CLI

def parse_selection(spec: str, maximum: int) -> list[int]:
    picked: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if part.lower() == "all":
            return list(range(1, maximum + 1))
        if "-" in part:
            a, b = part.split("-", 1)
            picked.update(range(int(a or 1), int(b or maximum) + 1))
        else:
            picked.add(int(part))
    bad = [i for i in picked if not 1 <= i <= maximum]
    if bad:
        raise SystemExit(f"Selection out of range 1-{maximum}: {sorted(bad)}")
    return sorted(picked)


def fmt_duration(sec) -> str:
    if not sec:
        return "  -  "
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def print_table(episodes: list[Episode]) -> None:
    for ep in episodes:
        print(f"{ep.index:4d}. {ep.season[:14]:<14} {ep.number:3d}  {fmt_duration(ep.duration):>7}  "
              f"{ep.title}  ({ep.idec})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", help="show URL (…/porady/<id>-<slug>/) or episode URL")
    p.add_argument("-l", "--list", action="store_true", help="only list episodes")
    p.add_argument("-s", "--select", help="episodes to download by list number, e.g. 1-5,8,12-")
    p.add_argument("--season", help="limit to a season, e.g. 'III', 'III. řada', 'Speciály'")
    p.add_argument("-i", "--interactive", action="store_true", help="list and ask what to download")
    p.add_argument("-q", "--quality", default="best",
                   help="best (default), worst, or one of " + ", ".join(QUALITY_ORDER[:5]))
    p.add_argument("-o", "--output", default=".", help="output directory (default: current)")
    p.add_argument("--subs", action="store_true", help="also save Czech subtitles (.vtt)")
    p.add_argument("--all-episodes", action="store_true",
                   help="include episodes not currently playable (they will fail to download)")
    p.add_argument("--overwrite", action="store_true", help="re-download existing files")
    p.add_argument("--keep-diacritics", action="store_true",
                   help="keep Czech characters and punctuation in file/directory names "
                        "(default: plain ASCII, e.g. 'Prasatko Peppa - III rada - 05 - Karavan')")
    p.add_argument("--test-seconds", type=int, metavar="N",
                   help="download only the first N seconds of each video (for testing)")
    args = p.parse_args()

    if not args.list and not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found – install it with: brew install ffmpeg")

    show_url, episode_idec = parse_url(args.url)
    programme = load_programme(show_url)
    show_title = clean(programme["title"])

    if episode_idec:  # single episode URL
        info = stream_info(episode_idec)
        ep = Episode(idec=episode_idec, title="", season="", duration=info.get("duration"))
        if args.list:
            print(f"{show_title}: {clean(info.get('episodeTitle') or '')} ({episode_idec})")
            return 0
        print(f"[1/1] {clean(info.get('episodeTitle') or '')}")
        download(ep, show_title, Path(args.output), args)
        return 0

    episodes = list_episodes(programme, args.season, not args.all_episodes)
    print(f"{show_title} – {len(episodes)} episodes")
    if not episodes:
        return 0

    if args.list:
        print_table(episodes)
        return 0
    if args.interactive:
        print_table(episodes)
        spec = input("\nEpisodes to download (e.g. 1-5,8 / all / empty to quit): ").strip()
        if not spec:
            return 0
        chosen = parse_selection(spec, len(episodes))
    elif args.select:
        chosen = parse_selection(args.select, len(episodes))
    else:
        chosen = list(range(1, len(episodes) + 1))

    failed = []
    for n, i in enumerate(chosen, 1):
        ep = episodes[i - 1]
        print(f"[{n}/{len(chosen)}] #{ep.index} {ep.season} {ep.number:02d} – {ep.title}")
        try:
            download(ep, show_title, Path(args.output), args)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130
        except Exception as e:  # keep going with the rest
            print(f"  FAILED: {e}")
            failed.append(ep)
    if failed:
        print(f"\n{len(failed)} failed: " + ", ".join(f"#{e.index}" for e in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
