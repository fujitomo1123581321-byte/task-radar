"""
課題締切リマインダースクリプト (check_deadlines.py)
- 未提出の課題の中から「締切が近いもの (24時間以内 / 3日以内)」および期限超過を検知
- PCにWindowsトースト通知を発行
- Discord Webhook経由でスマホへリマインダー通知を送信
  ※ スパム防止のため、定期通知は「朝(08:00 JST)」と「夜(19:00 JST)」の1日2回のみ配信
  ※ 複数メッセージを1つに統合してスッキリ配信
"""
import os
import sys
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# Fix Windows console encoding for Japanese and emoji
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE_DIR, "assignments.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LAST_NOTIFIED_PATH = os.path.join(BASE_DIR, "last_notified.json")

def load_data():
    assignments = []
    config = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        config["discord_webhook_url"] = os.environ.get("DISCORD_WEBHOOK_URL").strip()

    local = []
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                local = json.load(f)
        except Exception:
            local = []

    # 1. スマホやWebで行われた「完了」操作を確実に反映するため、通知前にクラウドから最新状態を取得
    cloud_endpoints = [
        "https://raw.githubusercontent.com/fujitomo1123581321-byte/task-radar/main/assignments.json",
        "https://task-radar-us.onrender.com/assignments.json"
    ]
    remote = None
    for url in cloud_endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TaskRadarDeadlineChecker/1.0", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=5) as res:
                if res.status == 200:
                    remote_data = json.loads(res.read().decode("utf-8"))
                    if isinstance(remote_data, list) and len(remote_data) > 0:
                        remote = remote_data
                        break
        except Exception:
            pass

    # 2. Smart merge: NEVER revert completed tasks from local or remote
    if remote and local:
        try:
            from discord_ai_bot import smart_merge_assignments
            assignments = smart_merge_assignments(local, remote)
        except Exception:
            assignments = remote
    elif remote:
        assignments = remote
    else:
        assignments = local

    # PCローカルのファイルも最新状態に自動更新（ズレ防止）
    if assignments:
        try:
            with open(JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(assignments, f, ensure_ascii=False, indent=2)
            js_path = os.path.join(BASE_DIR, "assignments.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write("window.INITIAL_ASSIGNMENTS = " + json.dumps(assignments, ensure_ascii=False, indent=2) + ";\n")
        except Exception:
            pass

    return assignments, config

def notify_windows(title, message):
    if sys.platform != 'win32':
        return
    try:
        import subprocess
        # PowerShell script for Windows Toast Notification
        ps_cmd = f"""
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $textNodes = $template.GetElementsByTagName("text")
        $textNodes.Item(0).AppendChild($template.CreateTextNode('{title}')) > $null
        $textNodes.Item(1).AppendChild($template.CreateTextNode('{message}')) > $null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('課題締切レーダー').Show($toast)
        """
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5)
    except Exception:
        pass

def send_consolidated_discord_notification(webhook_url, overdue, urgent_24h, urgent_3d, jst_now):
    if not webhook_url or not webhook_url.startswith("http"):
        return

    fields = []

    # 1. Overdue section (Max 4 items)
    if overdue:
        lines = []
        for a in overdue[:4]:
            tag = "[自分]" if a.get("category") == "personal" else "[学業]"
            sub = a.get("subject", "学業")
            dl = a.get("deadline", "").replace("T", " ")[:16]
            rem = a.get("remaining_text", "")
            lines.append(f"• **{tag} {sub}: {a.get('title')}**\n  💀 {rem} (締切: `{dl}`)")
        fields.append({
            "name": f"🚨 【期限超過】({len(overdue)}件)",
            "value": "\n".join(lines)[:1000],
            "inline": False
        })

    # 2. 24h Urgent section (Max 4 items)
    if urgent_24h:
        lines = []
        for a in urgent_24h[:4]:
            tag = "[自分]" if a.get("category") == "personal" else "[学業]"
            sub = a.get("subject", "学業")
            dl = a.get("deadline", "").replace("T", " ")[:16]
            rem = a.get("remaining_text", "")
            lines.append(f"• **{tag} {sub}: {a.get('title')}**\n  ⏰ **{rem}** (締切: `{dl}`)")
        fields.append({
            "name": f"⏰ 【24時間以内に締切】({len(urgent_24h)}件)",
            "value": "\n".join(lines)[:1000],
            "inline": False
        })

    # 3. 3-day Urgent section (Max 4 items)
    if urgent_3d:
        lines = []
        for a in urgent_3d[:4]:
            tag = "[自分]" if a.get("category") == "personal" else "[学業]"
            sub = a.get("subject", "学業")
            dl = a.get("deadline", "").replace("T", " ")[:16]
            rem = a.get("remaining_text", "")
            lines.append(f"• **{tag} {sub}: {a.get('title')}**\n  ⏳ {rem} (締切: `{dl}`)")
        fields.append({
            "name": f"📅 【3日以内に締切】({len(urgent_3d)}件)",
            "value": "\n".join(lines)[:1000],
            "inline": False
        })

    if not fields:
        return

    # Determine embed color and title
    if overdue:
        embed_color = 0x991B1B # Deep Red
        title_prefix = "🚨 【締切超過・至急確認】"
    elif urgent_24h:
        embed_color = 0xEF4444 # Red
        title_prefix = "⏰ 【本日・明日締切】"
    else:
        embed_color = 0xF59E0B # Amber
        title_prefix = "📅 【今週の提出リマインダー】"

    hour = jst_now.hour
    time_label = "朝の定期リマインド" if hour < 12 else ("夜の定期リマインド" if hour >= 18 else "定期リマインド")
    embed_title = f"{title_prefix} 課題・タスク状況 ({time_label})"

    payload = {
        "username": "🎯 課題・タスク提出期限レーダー",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2921/2921222.png",
        "embeds": [{
            "title": embed_title,
            "description": "未完了のタスク一覧です。提出後はダッシュボードで完了にしてね！\n🌐 [Webダッシュボードを開く](https://fujitomo1123581321-byte.github.io/task-radar/) • 🔗 [LETUSカレンダー](https://letus.ed.tus.ac.jp/calendar/view.php?view=upcoming)",
            "color": embed_color,
            "fields": fields,
            "footer": {
                "text": f"配信時刻: {jst_now.strftime('%H:%M')} JST • 毎朝8時と夜19時に配信"
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "AssignmentRadar/1.0"}
        )
        urllib.request.urlopen(req, timeout=10)
        print("  [Discord] 統合リマインダー通知を送信しました！")

        # Save last notified timestamp
        try:
            with open(LAST_NOTIFIED_PATH, "w", encoding="utf-8") as f:
                json.dump({"timestamp": time.time(), "time_str": jst_now.strftime("%Y-%m-%d %H:%M:%S")}, f)
        except Exception:
            pass
    except Exception as e:
        print(f"  [Discord送信エラー]: {e}")

def check_and_notify(force_discord=False):
    assignments, config = load_data()
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst).replace(tzinfo=None)

    urgent_24h = []
    urgent_3d = []
    overdue = []

    for a in assignments:
        if a.get("status") == "submitted":
            continue

        dl_str = a.get("deadline")
        if not dl_str:
            continue

        try:
            s = dl_str[:19]
            if len(s) == 16:
                s += ":00"
            dl_dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            try:
                dl_dt = datetime.fromisoformat(dl_str[:19])
            except Exception:
                continue

        diff = dl_dt - now
        total_seconds = diff.total_seconds()

        if total_seconds <= 0:
            # Overdue but unsubmitted
            days_ago = abs(int(total_seconds // 86400))
            a["remaining_text"] = f"{days_ago}日前に締切超過" if days_ago > 0 else "本日締切超過"
            a["total_seconds"] = total_seconds
            overdue.append(a)
            continue

        days = int(total_seconds // 86400)
        hours = int((total_seconds % 86400) // 3600)
        minutes = int((total_seconds % 3600) // 60)

        if days > 0:
            remaining_text = f"あと {days}日 {hours}時間"
        else:
            remaining_text = f"あと {hours}時間 {minutes}分"

        a["remaining_text"] = remaining_text
        a["total_seconds"] = total_seconds

        if total_seconds <= 24 * 3600:
            urgent_24h.append(a)
        elif total_seconds <= 3 * 24 * 3600:
            urgent_3d.append(a)

    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 締切チェック結果: 期限超過 {len(overdue)}件, 24時間以内 {len(urgent_24h)}件, 3日以内 {len(urgent_3d)}件")

    # 1. Windows PC Toast Notification (only if on local PC)
    if overdue:
        first = overdue[0]
        title = f"🚨【期限超過の未完了課題】{len(overdue)}件が締切を過ぎています"
        msg = f"「{first.get('subject')}: {first.get('title')}」 ({first.get('remaining_text')})"
        notify_windows(title, msg)
    elif urgent_24h:
        first = urgent_24h[0]
        title = f"🚨【提出期限が迫っています】あと{len(urgent_24h)}件が24時間以内！"
        msg = f"「{first.get('subject')}: {first.get('title')}」 ({first.get('remaining_text')})"
        notify_windows(title, msg)

    # 2. Discord Smartphone Notification (Smart Throttled)
    discord_url = config.get("discord_webhook_url", "").strip()
    if not discord_url:
        return

    # Check if we should send Discord notification now
    # Permitted hours in JST: Morning (8:00 - 8:59 JST) and Evening (19:00 - 19:59 JST)
    current_hour = now.hour
    is_in_schedule_window = current_hour in (8, 19)

    # Check 4-hour cooldown
    is_cooldown_ok = True
    if os.path.exists(LAST_NOTIFIED_PATH):
        try:
            with open(LAST_NOTIFIED_PATH, "r", encoding="utf-8") as f:
                last_data = json.load(f)
                if time.time() - last_data.get("timestamp", 0) < 4 * 3600:
                    is_cooldown_ok = False
        except Exception:
            pass

    if force_discord or (is_in_schedule_window and is_cooldown_ok):
        if overdue or urgent_24h or urgent_3d:
            send_consolidated_discord_notification(discord_url, overdue, urgent_24h, urgent_3d, now)
        else:
            print("  [Discord] 緊急の未提出課題はありません。通知をスキップしました。")
    else:
        if not is_in_schedule_window:
            print(f"  [Discord通知スキップ] 現在JST {current_hour}時。定期リマインダーは朝(8時)と夜(19時)のみ配信します（毎時スパム防止）。")
        elif not is_cooldown_ok:
            print("  [Discord通知スキップ] 直近4時間以内に送信済みのため重複防止でスキップしました。")

if __name__ == "__main__":
    force = "--force" in sys.argv
    check_and_notify(force_discord=force)
