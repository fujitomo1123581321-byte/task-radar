"""
LETUS (Moodle) 自動課題同期スクリプト (Pro Version)
- CATEGORIES から正確な履修科目名を自動特定
- 提出期限・受験終了イベントを抽出
- assignments.json & assignments.js を自動更新
- Windowsトースト通知
"""
import os
import sys
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
JSON_PATH = os.path.join(BASE_DIR, "assignments.json")
JS_PATH = os.path.join(BASE_DIR, "assignments.js")
IGNORED_PATH = os.path.join(BASE_DIR, "ignored_titles.json")

SUBJECT_MAP = {
    '物性論２Ａ': '01物性論2A',
    '物性論2A': '01物性論2A',
    '物性論２Ｂ': '02物性論2B',
    '物性論2B': '02物性論2B',
    '物性論２Ｃ': '02物性論2C',
    '物性論2C': '02物性論2C',
    '物性論３Ｂ': '02物性論3B',
    '物性論3B': '02物性論3B',
    '統計力学２': '02統計力学2',
    '統計力学2': '02統計力学2',
    '量子力学３': '03量子力学3',
    '量子力学3': '03量子力学3',
    '天体物理': '04天体物理学',
    '原子核物理': '05原子核物理',
    '数値計算': '07数値計算',
    '物理学実験': '08実験',
    '実験３': '08実験',
    '実験': '08実験',
    'ゼミ': '09ゼミ',
    '国際関係': '10国際関係論',
    'データサイエンス': '11データサイエンス・ＡＩ応用基礎',
}

def load_ignored_titles():
    titles = set()
    if os.path.exists(IGNORED_PATH):
        try:
            with open(IGNORED_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    titles.update(data)
        except Exception:
            pass
    return list(titles)

def load_config():
    cfg = {"letus_calendar_url": "", "notify_windows": sys.platform == "win32", "ignore_keywords": [], "ignored_titles": []}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # Merge persisted ignored_titles.json (especially crucial for GitHub Actions / Cloud)
    cfg["ignored_titles"] = list(set(cfg.get("ignored_titles", []) + load_ignored_titles()))

    # Prioritize environment variables (for GitHub Actions / Cloud)
    if os.environ.get("LETUS_CALENDAR_URL"):
        cfg["letus_calendar_url"] = os.environ.get("LETUS_CALENDAR_URL").strip()
    url_file = os.path.join(BASE_DIR, "letus_url.txt")
    if os.path.exists(url_file):
        try:
            with open(url_file, "r", encoding="utf-8") as f:
                saved = f.read().strip()
                if saved:
                    cfg["letus_calendar_url"] = saved
        except Exception:
            pass
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        cfg["discord_webhook_url"] = os.environ.get("DISCORD_WEBHOOK_URL").strip()
    return cfg

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def notify_windows(title, message):
    if sys.platform != "win32":
        return
    try:
        import subprocess
        ps_cmd = f"""
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $textNodes = $template.GetElementsByTagName("text")
        $textNodes.Item(0).AppendChild($template.CreateTextNode('{title}')) > $null
        $textNodes.Item(1).AppendChild($template.CreateTextNode('{message}')) > $null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('LETUS課題レーダー').Show($toast)
        """
        import subprocess
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5)
    except Exception:
        pass

def parse_ics_datetime(dt_str):
    dt_str = dt_str.strip()
    is_utc = dt_str.endswith('Z')
    clean_str = dt_str.rstrip('Z')
    
    try:
        if len(clean_str) >= 15:
            y = int(clean_str[0:4])
            m = int(clean_str[4:6])
            d = int(clean_str[6:8])
            hh = int(clean_str[9:11])
            mm = int(clean_str[11:13])
            ss = int(clean_str[13:15])
            
            if is_utc:
                utc_dt = datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)
                jst_dt = utc_dt.astimezone(timezone(timedelta(hours=9)))
                return jst_dt.strftime("%Y-%m-%dT%H:%M:%S")
            else:
                return f"{y:04d}-{m:02d}-{d:02d}T{hh:02d}:{mm:02d}:{ss:02d}"
        elif len(clean_str) == 8 and clean_str.isdigit():
            # All-day event (YYYYMMDD) -> default to 23:59:00 JST
            y = int(clean_str[0:4])
            m = int(clean_str[4:6])
            d = int(clean_str[6:8])
            # Validate valid month/day
            datetime(y, m, d)
            return f"{y:04d}-{m:02d}-{d:02d}T23:59:00"
    except (ValueError, OverflowError):
        return None
    return None

def resolve_subject(category_str, summary_str, desc_str):
    text = f"{category_str} {summary_str} {desc_str}"
    for k, v in SUBJECT_MAP.items():
        if k in text:
            return v
    # Fallback to category raw
    if category_str:
        clean = re.sub(r'\(.*?\)|（.*?）', '', category_str).strip()
        if clean:
            return clean
    return "学業"

def clean_title(raw_title):
    t = raw_title.strip()
    t = t.replace(r"\,", ",").replace(r"\;", ";").replace(r"\n", " ")
    # Moodle format: 「タイトル」の提出期限 -> タイトル
    m = re.match(r"^「(.+?)」の提出期限$", t)
    if m:
        return m.group(1).strip()
    # Moodle format: 「タイトル」終了 -> タイトル
    m = re.match(r"^「(.+?)」終了$", t)
    if m:
        return m.group(1).strip()
    # イベント名 の受験可能期間の終了 -> イベント名
    t = re.sub(r"\s*の受験可能期間の終了$", "", t)
    t = re.sub(r"\s*の提出期限$", "", t)
    t = re.sub(r"\s*が期限です。?$", "", t)
    return t.strip()

def fetch_and_sync():
    cfg = load_config()
    url = cfg.get("letus_calendar_url", "").strip()
    
    if not url:
        print("LETUSカレンダーURLが設定されていません。")
        return {"new_items": [], "updated_count": 0, "error": "No URL"}

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] LETUSから最新課題を取得中...")
    
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AssignmentRadar/1.0"}
    )
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ics_text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"取得エラー: {e}")
        return

    # Load existing assignments
    existing = []
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    # RFC 5545 Unfolding: merge multi-line folded fields
    ics_text = re.sub(r"\r?\n[ \t]", "", ics_text)
    events = ics_text.split("BEGIN:VEVENT")
    new_items = []
    updated_count = 0
    # Always use JST for consistent deadline comparison across all platforms (Windows / GitHub Actions)
    now = datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)

    for ev in events[1:]:
        summary_m = re.search(r"SUMMARY:(.+)", ev)
        dtend_m = re.search(r"DTEND(?:;[^:]+)?:(\d{8}T\d{6}Z?|\d{8})", ev) or re.search(r"DTSTART(?:;[^:]+)?:(\d{8}T\d{6}Z?|\d{8})", ev)
        desc_m = re.search(r"DESCRIPTION:(.+)", ev)
        cat_m = re.search(r"CATEGORIES:(.+)", ev)
        uid_m = re.search(r"UID:(.+)", ev)

        if not (summary_m and dtend_m):
            continue

        raw_summary = summary_m.group(1).strip()
        raw_category = cat_m.group(1).strip() if cat_m else ""
        raw_desc = desc_m.group(1).strip() if desc_m else ""
        raw_desc = raw_desc.replace(r"\n", " ").replace(r"\,", ",")
        uid = uid_m.group(1).strip() if uid_m else ""

        # Skip start notifications (e.g. 「〜の受験可能期間の開始」, 「〜」開始)
        # Avoid falsely skipping legitimate titles like 「実験開始前レポート」
        is_start_event = (
            "の受験可能期間の開始" in raw_summary or
            raw_summary.endswith("」開始") or
            raw_summary.endswith(" 開始")
        )
        if is_start_event:
            continue

        raw_dtend = dtend_m.group(1).strip()
        deadline = parse_ics_datetime(raw_dtend)
        if not deadline:
            continue

        title = clean_title(raw_summary)

        # Check ignore filters
        ignore_kw = cfg.get("ignore_keywords", [])
        ignored_titles = cfg.get("ignored_titles", [])
        combined_text = f"{raw_summary} {title} {raw_desc}"
        if any(k in combined_text for k in ignore_kw) or title in ignored_titles:
            continue
        subject = resolve_subject(raw_category, title, raw_desc)

        # Check if already in list
        found = False
        for item in existing:
            # Match strictly by UID or (same title AND same subject)
            item_uid = item.get("moodleUid", "").strip() if item.get("moodleUid") else ""
            item_title = item.get("title", "").strip() if item.get("title") else ""
            is_same_uid = bool(uid and item_uid and item_uid == uid)
            is_same_task = (
                item.get("category") == "academic"
                and item_title == title.strip()
                and (item.get("subject") == subject or item.get("subject") == "学業")
            )
            if is_same_uid or is_same_task:
                found = True
                # If subject was generic "学業", upgrade it
                if item.get("subject") == "学業" and subject != "学業":
                    item["subject"] = subject
                if item.get("deadline") != deadline:
                    item["deadline"] = deadline
                    updated_count += 1
                break

        if not found:
            # Check deadline vs now
            # Only consider ancient archived tasks (> 14 days in past) as submitted.
            # Recent overdue tasks (< 14 days past) stay not_started so student never misses them!
            dl_dt = datetime.strptime(deadline, "%Y-%m-%dT%H:%M:%S")
            is_ancient = dl_dt < (now - timedelta(days=14))

            new_item = {
                "id": "letus-" + datetime.now().strftime("%Y%m%d%H%M%S") + f"-{len(new_items)}",
                "category": "academic",
                "subject": subject,
                "title": title,
                "deadline": deadline,
                "submissionMethod": "LETUS",
                "status": "submitted" if is_ancient else "not_started",
                "priority": "high" if "テスト" in title or "レポート" in title else "medium",
                "memo": raw_desc[:200] if raw_desc else "LETUSより自動同期",
                "folderPath": None,
                "createdAt": datetime.now().isoformat(),
                "completedAt": datetime.now().isoformat() if is_ancient else None,
                "moodleUid": uid
            }
            new_items.append(new_item)
            try:
                print(f"  [新着追加] {subject}: {title} (締切: {deadline})")
            except Exception:
                pass

    if new_items:
        existing.extend(new_items)

    # Clean up duplicate old sample placeholders if we now have real LETUS items
    # Save assignments.json
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    # Save assignments.js
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.INITIAL_ASSIGNMENTS = " + json.dumps(existing, ensure_ascii=False, indent=2) + ";\n")

    print(f"\n同期完了: 新着 {len(new_items)} 件, 更新 {updated_count} 件")
    
    if new_items:
        upcoming_new = [x for x in new_items if x["status"] != "submitted"]
        if upcoming_new and cfg.get("notify_windows", True):
            notify_windows(
                "📥 LETUS新着課題が同期されました",
                f"{len(upcoming_new)}件の提出予定課題が追加されました！"
            )

    # Auto-push to GitHub if git is available and changes were made
    if new_items or updated_count > 0:
        try:
            import subprocess
            files = ["assignments.json", "assignments.js"]
            if os.path.exists(IGNORED_PATH):
                files.append("ignored_titles.json")
            status = subprocess.run(["git", "status", "--porcelain"] + files, cwd=BASE_DIR, capture_output=True, text=True)
            if any(f in status.stdout for f in files):
                subprocess.run(["git", "add"] + files, cwd=BASE_DIR, check=True, capture_output=True)
                msg = f"Auto-sync LETUS assignments (New: {len(new_items)}, Updated: {updated_count})"
                subprocess.run(["git", "commit", "-m", msg], cwd=BASE_DIR, check=True, capture_output=True)
                subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, check=True, capture_output=True)
                print("🚀 GitHubへ自動プッシュしました！")
        except Exception:
            pass

    # Also check imminent deadlines and notify
    try:
        import check_deadlines
        check_deadlines.check_and_notify()
    except Exception as e:
        pass

    return {"new_items": new_items, "updated_count": updated_count}

if __name__ == "__main__":
    fetch_and_sync()
