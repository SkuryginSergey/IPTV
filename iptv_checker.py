#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV Checker — проверка работоспособности потоков из плейлиста M3U.

Что делает:
  * разбирает M3U (#EXTINF, #EXTVLCOPT с user-agent / referrer, группы);
  * проверяет каждый поток параллельно:
      - HLS (.m3u8): скачивает плейлист, при master-плейлисте переходит
        в вариант с максимальным качеством, затем реально запрашивает
        видеосегмент — канал считается рабочим, только если отдаёт видео;
      - DASH (.mpd): проверяет, что отдаётся корректный MPD-манифест;
      - прочие (mpegts, mp4, http-потоки): читает первые байты ответа;
  * печатает итог в консоль и сохраняет:
      - <имя>_report.html — наглядный отчёт по группам (открыть в браузере);
      - <имя>_report.csv  — таблица для Excel;
      - <имя>_working.m3u — плейлист только из рабочих каналов.

Только стандартная библиотека Python 3.8+, ничего устанавливать не нужно.

Примеры:
  python iptv_checker.py IPTV_work.m3u
  python iptv_checker.py IPTV_work.m3u --group "Федеральные" --group "Федеральные +4"
  python iptv_checker.py IPTV_work.m3u --workers 30 --timeout 8
"""

import argparse
import csv
import html
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Многие IPTV-серверы используют самоподписанные/просроченные сертификаты —
# для проверки доступности это не важно.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ----------------------------------------------------------------- модель ---

@dataclass
class Channel:
    index: int
    name: str
    group: str
    url: str
    extinf: str
    extra_lines: List[str] = field(default_factory=list)
    user_agent: Optional[str] = None
    referrer: Optional[str] = None
    # результат проверки
    ok: bool = False
    status: str = ""
    detail: str = ""
    quality: str = ""
    elapsed: float = 0.0


# ----------------------------------------------------------------- парсер ---

def parse_m3u(path: str):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = [l.rstrip("\r\n") for l in f]

    header, channels = [], []
    cur = None
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', s)
            name = s.rsplit(",", 1)[-1].strip() if "," in s else "?"
            cur = Channel(index=len(channels) + 1, name=name,
                          group=m.group(1) if m else "", url="", extinf=s)
            continue
        if cur is None:
            if s.startswith("#EXTM3U"):
                header.append(s)
            continue
        if s.startswith("#"):
            cur.extra_lines.append(s)
            low = s.lower()
            if low.startswith("#extvlcopt:http-user-agent="):
                cur.user_agent = s.split("=", 1)[1]
            elif low.startswith("#extvlcopt:http-referrer=") or low.startswith("#extvlcopt:http-referer="):
                cur.referrer = s.split("=", 1)[1]
            continue
        # строка с URL
        cur.url = s
        channels.append(cur)
        cur = None
    return header, channels


# ---------------------------------------------------------------- проверка ---

class CheckError(Exception):
    pass


def http_get(url, ch: Channel, timeout, max_bytes=None, rng=None):
    headers = {"User-Agent": ch.user_agent or DEFAULT_UA, "Accept": "*/*"}
    if ch.referrer:
        headers["Referer"] = ch.referrer
    if rng:
        headers["Range"] = rng
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
    except urllib.error.HTTPError as e:
        raise CheckError(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise CheckError(classify_net_error(reason))
    except Exception as e:  # noqa
        raise CheckError(classify_net_error(e))
    try:
        data = resp.read(max_bytes) if max_bytes else resp.read(2_000_000)
    except Exception as e:  # noqa
        raise CheckError(classify_net_error(e))
    finally:
        resp.close()
    return resp.geturl(), resp.headers.get("Content-Type", ""), data


def classify_net_error(e):
    t = str(e).lower()
    if "timed out" in t or "timeout" in t:
        return "Таймаут"
    if "name or service" in t or "getaddrinfo" in t or "nodename" in t or "11001" in t:
        return "DNS: хост не найден"
    if "refused" in t or "10061" in t:
        return "Соединение отклонено"
    if "reset" in t or "10054" in t:
        return "Соединение сброшено"
    if "ssl" in t or "certificate" in t:
        return "Ошибка SSL"
    return str(e)[:80]


def best_variant(base, text):
    """Из master-плейлиста выбирает вариант с максимальным bandwidth."""
    best, best_bw, best_res = None, -1, ""
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-STREAM-INF"):
            bw = re.search(r"BANDWIDTH=(\d+)", l)
            res = re.search(r"RESOLUTION=(\d+x\d+)", l)
            bw = int(bw.group(1)) if bw else 0
            for nxt in lines[i + 1:]:
                nxt = nxt.strip()
                if nxt and not nxt.startswith("#"):
                    if bw >= best_bw:
                        best, best_bw = urllib.parse.urljoin(base, nxt), bw
                        best_res = res.group(1) if res else ""
                    break
    return best, best_res


def first_segment(base, text):
    segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not segs:
        return None
    # последний сегмент — самый «свежий» в live-плейлисте
    return urllib.parse.urljoin(base, segs[-1])


def quality_label(res):
    if not res:
        return ""
    try:
        h = int(res.split("x")[1])
    except Exception:
        return res
    tag = "4K" if h >= 2000 else "FHD" if h >= 1000 else "HD" if h >= 700 else "SD"
    return f"{res} ({tag})"


def check_hls(ch, timeout, base, text):
    quality = ""
    if "#EXT-X-STREAM-INF" in text:
        var, res = best_variant(base, text)
        quality = quality_label(res)
        if not var:
            raise CheckError("Пустой master-плейлист")
        base, _, data = http_get(var, ch, timeout)
        text = data.decode("utf-8", "replace")
        if "#EXTM3U" not in text:
            raise CheckError("Вариант не является плейлистом")
    seg = first_segment(base, text)
    if not seg:
        raise CheckError("Нет сегментов в плейлисте")
    _, ctype, data = http_get(seg, ch, timeout, max_bytes=4096, rng="bytes=0-4095")
    if len(data) < 188:
        raise CheckError("Сегмент пустой")
    if ctype.startswith("text/html") or data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        raise CheckError("Вместо видео — HTML-страница")
    return quality, "видео отдаётся"


def check_dash(ch, timeout, text):
    if "<MPD" not in text:
        raise CheckError("Ответ не является MPD")
    hs = [int(h) for h in re.findall(r'\bheight="(\d+)"', text)]
    q = quality_label(f"?x{max(hs)}") if hs else ""
    return q.replace("?x", "").replace(" (", "p (", 1) if q else "", "манифест DASH"


def check_channel(ch: Channel, timeout: float):
    t0 = time.time()
    try:
        url = ch.url
        if not url.lower().startswith(("http://", "https://")):
            raise CheckError("Неподдерживаемый протокол")
        final, ctype, data = http_get(url, ch, timeout, max_bytes=2_000_000)
        head = data[:4096].decode("utf-8", "replace")
        if "#EXTM3U" in head:
            ch.quality, ch.detail = check_hls(ch, timeout, final, data.decode("utf-8", "replace"))
        elif "<MPD" in head or url.lower().split("?")[0].endswith(".mpd"):
            ch.quality, ch.detail = check_dash(ch, timeout, data.decode("utf-8", "replace"))
        else:
            if len(data) < 188:
                raise CheckError("Пустой ответ")
            if ctype.startswith("text/html") or head.lstrip().lower().startswith(("<!doctype", "<html")):
                raise CheckError("Вместо видео — HTML-страница")
            ch.detail = "поток отдаёт данные" + (" (MPEG-TS)" if data[:1] == b"\x47" else "")
        ch.ok, ch.status = True, "OK"
    except CheckError as e:
        ch.ok, ch.status, ch.detail = False, "FAIL", str(e)
    except Exception as e:  # noqa
        ch.ok, ch.status, ch.detail = False, "FAIL", f"Ошибка: {e}"[:100]
    ch.elapsed = time.time() - t0
    return ch


# ------------------------------------------------------------------ вывод ---

def enable_win_colors():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


G, R, Y, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def write_csv(path, chans):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["№", "Группа", "Канал", "Статус", "Качество", "Подробности", "Время, c", "URL"])
        for c in chans:
            w.writerow([c.index, c.group, c.name, "Работает" if c.ok else "Не работает",
                        c.quality, c.detail, f"{c.elapsed:.1f}", c.url])


def write_m3u(path, header, chans):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header or ["#EXTM3U"]) + "\n")
        for c in chans:
            f.write("\n" + c.extinf + "\n")
            for l in c.extra_lines:
                f.write(l + "\n")
            f.write(c.url + "\n")


def write_html(path, src, chans, started):
    ok = sum(c.ok for c in chans)
    groups = {}
    for c in chans:
        groups.setdefault(c.group or "Без группы", []).append(c)
    e = html.escape
    parts = [f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Проверка IPTV</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7280;--ok:#15803d;--bad:#b91c1c;--line:#e5e7eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111418;--card:#1a1f26;--fg:#e6e9ee;--mut:#9aa3af;--ok:#4ade80;--bad:#f87171;--line:#2a313a}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif}}
.wrap{{max-width:1100px;margin:auto}} h1{{margin:0 0 4px;font-size:22px}} .mut{{color:var(--mut)}}
.sum{{display:flex;gap:12px;margin:16px 0;flex-wrap:wrap}} .tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px}}
.tile b{{font-size:22px;display:block}} input,select{{padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}}
details{{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:10px 0}} summary{{padding:10px 14px;cursor:pointer;font-weight:600}}
table{{width:100%;border-collapse:collapse}} td,th{{padding:6px 10px;border-top:1px solid var(--line);text-align:left;vertical-align:top}}
th{{color:var(--mut);font-weight:500}} .ok{{color:var(--ok)}} .bad{{color:var(--bad)}} .url{{word-break:break-all;color:var(--mut);font-size:12px}}
</style></head><body><div class="wrap">
<h1>Проверка IPTV-потоков</h1><div class="mut">{e(os.path.basename(src))} · {time.strftime('%d.%m.%Y %H:%M', time.localtime(started))}</div>
<div class="sum"><div class="tile"><b>{len(chans)}</b>всего</div><div class="tile ok"><b>{ok}</b>работают</div>
<div class="tile bad"><b>{len(chans)-ok}</b>не работают</div></div>
<p><input id="q" placeholder="Поиск канала" oninput="f()"> <select id="s" onchange="f()">
<option value="">Все</option><option value="ok">Только рабочие</option><option value="bad">Только нерабочие</option></select></p>"""]
    for g, cs in groups.items():
        gok = sum(c.ok for c in cs)
        parts.append(f'<details open><summary>{e(g)} <span class="mut">— {gok} из {len(cs)}</span></summary><table>'
                     '<tr><th>Канал</th><th>Статус</th><th>Качество</th><th>Подробности</th></tr>')
        for c in cs:
            cls = "ok" if c.ok else "bad"
            parts.append(f'<tr data-s="{cls}"><td>{e(c.name)}<div class="url">{e(c.url)}</div></td>'
                         f'<td class="{cls}">{"● работает" if c.ok else "✕ не работает"}</td>'
                         f'<td>{e(c.quality)}</td><td class="mut">{e(c.detail)}</td></tr>')
        parts.append("</table></details>")
    parts.append("""<script>function f(){var q=document.getElementById('q').value.toLowerCase(),s=document.getElementById('s').value;
document.querySelectorAll('tr[data-s]').forEach(function(r){var v=(!s||r.dataset.s==s)&&r.textContent.toLowerCase().indexOf(q)>=0;r.style.display=v?'':'none'})}</script>
</div></body></html>""")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))


# ------------------------------------------------------------------- main ---

def main():
    enable_win_colors()
    ap = argparse.ArgumentParser(description="Проверка работоспособности IPTV-потоков из M3U")
    ap.add_argument("playlist", help="путь к файлу .m3u / .m3u8")
    ap.add_argument("-w", "--workers", type=int, default=20, help="параллельных проверок (по умолчанию 20)")
    ap.add_argument("-t", "--timeout", type=float, default=10, help="таймаут запроса, сек (по умолчанию 10)")
    ap.add_argument("-g", "--group", action="append", help="проверять только эту группу (можно несколько раз)")
    ap.add_argument("-o", "--out-dir", help="куда сохранить результаты (по умолчанию — рядом с плейлистом)")
    ap.add_argument("--only-failed", action="store_true", help="в консоли показывать только нерабочие")
    args = ap.parse_args()

    if not os.path.isfile(args.playlist):
        sys.exit(f"Файл не найден: {args.playlist}")
    header, chans = parse_m3u(args.playlist)
    if args.group:
        chans = [c for c in chans if c.group in args.group]
    if not chans:
        sys.exit("В плейлисте не найдено каналов для проверки.")

    print(f"Каналов к проверке: {len(chans)}  (потоков: {args.workers}, таймаут: {args.timeout} с)\n")
    started = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(check_channel, c, args.timeout) for c in chans]
        for fut in as_completed(futs):
            c = fut.result()
            done += 1
            if c.ok and args.only_failed:
                continue
            mark = f"{G}OK  {RST}" if c.ok else f"{R}FAIL{RST}"
            q = f" {Y}{c.quality}{RST}" if c.quality else ""
            print(f"[{done:>4}/{len(chans)}] {mark} {c.name[:40]:<40}{q} {DIM}{c.group} · {c.detail}{RST}")

    chans.sort(key=lambda c: c.index)
    ok = [c for c in chans if c.ok]
    base = os.path.splitext(os.path.basename(args.playlist))[0]
    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.playlist))
    os.makedirs(out_dir, exist_ok=True)
    p_html = os.path.join(out_dir, f"{base}_report.html")
    p_csv = os.path.join(out_dir, f"{base}_report.csv")
    p_m3u = os.path.join(out_dir, f"{base}_working.m3u")
    write_html(p_html, args.playlist, chans, started)
    write_csv(p_csv, chans)
    write_m3u(p_m3u, header, ok)

    print(f"\nГотово за {time.time() - started:.0f} с. "
          f"Работают: {G}{len(ok)}{RST}, не работают: {R}{len(chans) - len(ok)}{RST} из {len(chans)}.\n")
    print("Итог по группам:")
    groups = {}
    for c in chans:
        g = groups.setdefault(c.group or "Без группы", [0, 0])
        g[0] += c.ok
        g[1] += 1
    for g, (a, b) in groups.items():
        print(f"  {g:<28} {a:>4} / {b}")
    print(f"\nОтчёт:            {p_html}\nТаблица:          {p_csv}\nРабочий плейлист: {p_m3u}")


if __name__ == "__main__":
    main()
