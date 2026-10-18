"""
課題締切リマインダースクリプト (check_deadlines.py)
- 未提出の課題の中から「締切が近いもの (24時間以内 / 3日以内)」を検知
- PCにWindowsトースト通知を発行
- (設定があれば) Discord / LINE / Webhook 経由でスマホにも通知を送信
"""
import os
import sys
import json
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

def load_data():
    assignments = []
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                assignments = json.load(f)
        except Exception:
            pass
            
    config = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        config["discord_webhook_url"] = os.environ.get("DISCORD_WEBHOOK_URL").strip()
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

def notify_discord(webhook_url, title, items):
    if not webhook_url or not webhook_url.startswith("http"):
        return
    try:
        fields = []
        for a in items[:8]:
            tag = "[自分]" if a.get("category") == "personal" else "[学業]"
            subject = a.get("subject", "学業")
            dl = a.get("deadline", "").replace("T", " ")[:16]
            rem = a.get("remaining_text", "")
            method = a.get("submissionMethod", "LETUS")
            
            fname = f"📌 {tag} {subject}: {a.get('title')}"
            if len(fname) > 250:
                fname = fname[:247] + "..."
            fval = f"⏳ **{rem}** | 📅 締切: `{dl}` | 提出先: {method}"
            if len(fval) > 1000:
                fval = fval[:997] + "..."
            fields.append({
                "name": fname,
                "value": fval,
                "inline": False
            })

        is_overdue = any(a.get("total_seconds", 1) <= 0 for a in items)
        is_emergency = any(0 < a.get("total_seconds", 999999) <= 24 * 3600 for a in items)
        embed_color = 0x991B1B if is_overdue else (0xEF4444 if is_emergency else 0xF59E0B) # Deep red for overdue, red for emergency, amber for 3d

        description = "⚠️ **以下の課題・タスクは既に提出期限を過ぎています！**\n提出済みならダッシュボードで完了にし、未提出なら至急確認してください。\n🔗 [LETUSカレンダーを開く](https://letus.ed.tus.ac.jp/calendar/view.php?view=upcoming)" if is_overdue else "未提出の課題・タスク一覧です。提出はお早めに！\n🔗 [LETUSカレンダーを開く](https://letus.ed.tus.ac.jp/calendar/view.php?view=upcoming)"

        payload = {
            "username": "🎯 課題・タスク提出期限レーダー",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/2921/2921222.png",
            "embeds": [{
                "title": title,
                "description": description,
                "color": embed_color,
                "fields": fields,
                "footer": {
                    "text": "提出後はダッシュボードで完了にしてね！ • 課題提出期限レーダー"
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }]
        }
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "AssignmentRadar/1.0"}
        )
        urllib.request.urlopen(req, timeout=10)
        print("  [Discord] スマホ宛てに通知を送信しました！")
    except Exception as e:
        print(f"  [Discord送信エラー]: {e}")

def test_discord(webhook_url):
    try:
        payload = {
            "username": "🎯 課題・タスク提出期限レーダー",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/2921/2921222.png",
            "embeds": [{
                "title": "🎉 Discord通知の連携に成功しました！",
                "description": "このチャンネルに、締切が近い未提出課題が自動で届くようになります。\nスマホのDiscordアプリでこのチャンネルの通知を「すべてのメッセージ」にしておくと、外出先でもロック画面に通知が届きます！",
                "color": 0x10B981, # Emerald
                "footer": {"text": "課題提出期限レーダー 連携テスト"}
            }]
        }
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "AssignmentRadar/1.0"}
        )
        urllib.request.urlopen(req, timeout=10)
        print("テスト通知をDiscordに送信しました！")
        return True
    except Exception as e:
        print(f"Discordテスト送信エラー: {e}")
        return False

def check_and_notify():
    assignments, config = load_data()
    # Always use Japan Standard Time (JST, UTC+9) regardless of host OS timezone
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
            dl_dt = datetime.strptime(dl_str[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue

        diff = dl_dt - now
        total_seconds = diff.total_seconds()

        if total_seconds <= 0:
            # Overdue but unsubmitted
            days_ago = abs(int(total_seconds // 86400))
            a["remaining_text"] = f"⚠️ {days_ago}日前に締切超過" if days_ago > 0 else "⚠️ 本日締切超過"
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

    # 1. Windows PC Toast Notification
    if overdue:
        first = overdue[0]
        title = f"🚨【期限超過の未完了課題】{len(overdue)}件が締切を過ぎています"
        msg = f"「{first.get('subject')}: {first.get('title')}」 ({first.get('remaining_text')})"
        print(f"  [PC通知] {title} - {msg}")
        notify_windows(title, msg)
    elif urgent_24h:
        first = urgent_24h[0]
        title = f"🚨【提出期限が迫っています】あと{len(urgent_24h)}件が24時間以内！"
        msg = f"「{first.get('subject')}: {first.get('title')}」 ({first.get('remaining_text')})"
        print(f"  [PC通知] {title} - {msg}")
        notify_windows(title, msg)
    elif urgent_3d:
        first = urgent_3d[0]
        title = f"⚠️【今週提出の課題】あと{len(urgent_3d)}件が3日以内です"
        msg = f"「{first.get('subject')}: {first.get('title')}」 ({first.get('remaining_text')})"
        print(f"  [PC通知] {title} - {msg}")
        notify_windows(title, msg)
    else:
        print("  直近の緊急課題はありません。順調です！")

    # 2. Discord / Smartphone Notification (if webhook URL configured)
    discord_url = config.get("discord_webhook_url", "").strip()
    if discord_url:
        if overdue:
            notify_discord(discord_url, f"🚨【提出期限超過】未提出の課題・タスクが {len(overdue)} 件あります！", overdue)
        if urgent_24h:
            notify_discord(discord_url, "🚨【提出期限アラート】24時間以内に締切の課題があります！", urgent_24h)
        elif urgent_3d:
            notify_discord(discord_url, "⚠️【提出期限リマインド】3日以内に締切の課題があります", urgent_3d)

if __name__ == "__main__":
    check_and_notify()
