"""
LETUS 提出状況 自動スキャナー＆誤完了セーフガード (完全READ-ONLY)
- LETUSの課題ページを開き、「緑色 (評定のために提出済み)」を読み取って自動判定
- LETUS側のボタン操作・フォーム送信・データ変更は一切行いません (閲覧のみ)
- 判定結果を assignments.json と assignments.js に反映し、クラウドおよびGitと自動同期
- 【朝一番スマート判定】朝(06:00-12:00)のPC初回オープン時のみ自動スキャンし、昼以降や2回目以降はスキップ
- 【誤完了セーフガード】Web上で「完了」にしたがLETUSで未提出の課題を検知した場合、未完了に復元＆Windows警告通知
"""
import os
import sys
import json
import re
import time
import argparse
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE_DIR, "assignments.json")
JS_PATH = os.path.join(BASE_DIR, "assignments.js")
BROWSER_PROFILE_DIR = os.path.join(BASE_DIR, ".letus_browser_profile")
LAST_MORNING_SCAN_PATH = os.path.join(BASE_DIR, "last_morning_scan.json")
CLOUD_API_URL = "https://task-radar-us.onrender.com/api/task-status"

# Console UTF-8 fix for Windows
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def notify_windows(title, message):
    """Windows トースト通知を発行 (画面右下に通知カードを表示)"""
    if sys.platform != 'win32':
        return
    try:
        # PowerShell script for Windows Toast Notification
        ps_cmd = f"""
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $textNodes = $template.GetElementsByTagName("text")
        $textNodes.Item(0).AppendChild($template.CreateTextNode('{title}')) > $null
        $textNodes.Item(1).AppendChild($template.CreateTextNode('{message}')) > $null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('LETUS課題提出チェッカー').Show($toast)
        """
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5)
    except Exception:
        pass

def load_assignments():
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_assignments(assignments):
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(assignments, f, ensure_ascii=False, indent=2)
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.INITIAL_ASSIGNMENTS = " + json.dumps(assignments, ensure_ascii=False, indent=2) + ";\n")

def sync_to_cloud(task_id, status, title=""):
    """Render APIにステータス変更を即座にPOST通知"""
    try:
        payload = json.dumps({"id": task_id, "status": status, "title": title}).encode("utf-8")
        req = urllib.request.Request(
            CLOUD_API_URL,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=7) as res:
            return res.status == 200
    except Exception as e:
        print(f"  [Cloud Sync Info] クラウドAPI同期スキップ ({e})")
        return False

def sync_to_git(summary_msg):
    """変更があれば自動でgit commit & push"""
    try:
        subprocess.run(["git", "add", "assignments.json", "assignments.js"], cwd=BASE_DIR, capture_output=True, timeout=10)
        commit_res = subprocess.run(
            ["git", "commit", "-m", f"auto: {summary_msg}"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=10
        )
        if "nothing to commit" not in commit_res.stdout.lower() and commit_res.returncode == 0:
            print(f"  [Git] コミット作成: auto: {summary_msg}")
            push_res = subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, capture_output=True, text=True, timeout=25)
            if push_res.returncode == 0:
                print("  [Git] GitHubへのプッシュ完了")
    except Exception as e:
        print(f"  [Git Sync Info] Gitプッシュスキップ ({e})")

def is_morning_eligible(force=False):
    """
    朝一番スマート判定:
    - 午前中 (06:00〜12:00) かつ「本日未実行」の場合のみ True
    - 昼以降や、今日すでに1回実行済みの場合は False
    """
    if force:
        return True, "強制実行モード"

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 1. 時間帯チェック (06:00 - 12:00)
    if not (6 <= now.hour < 12):
        return False, f"現在時刻({now.strftime('%H:%M')})は午前中(06:00〜12:00)ではないためスキップします。"

    # 2. 本日実行済みチェック
    if os.path.exists(LAST_MORNING_SCAN_PATH):
        try:
            with open(LAST_MORNING_SCAN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("last_date") == today_str:
                    return False, f"本日({today_str})朝のスキャンは既に完了しているためスキップします (今日2回目の起動)。"
        except Exception:
            pass

    return True, "朝の初回スキャンを実行します。"

def record_morning_scan_done():
    """本日朝のスキャン完了日を記録"""
    now = datetime.now()
    data = {
        "last_date": now.strftime("%Y-%m-%d"),
        "last_run": now.isoformat()
    }
    try:
        with open(LAST_MORNING_SCAN_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def scan_letus_submissions(headless=False, morning_check_only=False, force=False):
    print("=" * 60)
    print("  🔍 LETUS提出状況 自動スキャナー＆誤完了セーフガード (READ-ONLY)")
    print("  ※LETUSへの変更・送信操作は一切行いません（閲覧のみ）")
    print("=" * 60)

    # 朝スマート判定
    if morning_check_only:
        eligible, reason = is_morning_eligible(force=force)
        print(f"[朝判定] {reason}")
        if not eligible:
            print("処理を終了します (スマート終了)。")
            return

    assignments = load_assignments()
    if not assignments:
        print("課題データが存在しません。")
        return

    # 対象1: 未提出の学業課題 (提出済みに自動昇格させる候補)
    unsubmitted_tasks = [
        a for a in assignments 
        if a.get("category") == "academic" and a.get("status") != "submitted"
    ]

    # 対象2: 直近の提出済み学業課題 (誤完了セーフガード: 未提出なのに完了になっていないか検査)
    now_dt = datetime.now()
    completed_check_candidates = []
    for a in assignments:
        if a.get("category") == "academic" and a.get("status") == "submitted":
            # 締切が直近（過去3日以内〜未来7日以内）または完了後間もない課題を検査対象にする
            dl_str = a.get("deadline")
            comp_str = a.get("completedAt")
            is_recent = False
            
            if dl_str:
                try:
                    dl_dt = datetime.fromisoformat(dl_str.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
                    # 締切の前後チェック (過去3日〜今後7日)
                    if -3 <= (dl_dt - now_dt).days <= 7:
                        is_recent = True
                except Exception:
                    pass

            if not is_recent and comp_str:
                try:
                    comp_dt = datetime.fromisoformat(comp_str.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
                    if (now_dt - comp_dt).days <= 3:
                        is_recent = True
                except Exception:
                    pass

            if is_recent or a.get("letusUrl"):
                completed_check_candidates.append(a)

    print(f"未提出の学業課題: {len(unsubmitted_tasks)} 件")
    print(f"誤完了チェック対象（直近の完了課題）: {len(completed_check_candidates)} 件\n")

    if not unsubmitted_tasks and not completed_check_candidates:
        print("確認対象の課題がありません。")
        if morning_check_only:
            record_morning_scan_done()
        return

    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)

    updated_promoted = []  # 未完了 -> 提出済みに自動昇格
    updated_reverted = []  # 誤完了検知 -> 未完了に自動復元

    with sync_playwright() as p:
        print(f"ブラウザを起動しています (Edge, headless={headless})...")
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=BROWSER_PROFILE_DIR,
                channel="msedge",
                headless=headless,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"]
            )
        except Exception as e:
            print(f"ブラウザ起動エラー (Edgeが既に開いている可能性があります): {e}")
            return

        page = context.new_page()

        # Step 1: Open upcoming events calendar
        print("\n[ステップ 1] LETUSの今後のイベント画面を開いています...")
        try:
            page.goto("https://letus.ed.tus.ac.jp/calendar/view.php?view=upcoming", wait_until="load", timeout=30000)
            time.sleep(3)
        except Exception as e:
            print(f"カレンダー接続エラー: {e}")
            context.close()
            return

        # Check if login is needed (Redirected away from letus.ed.tus.ac.jp/calendar)
        is_calendar = "letus.ed.tus.ac.jp" in page.url and "calendar" in page.url

        if not is_calendar:
            print(f"\n⚠️ 【LETUSのログインが必要です】(現在URL: {page.url})")
            if headless:
                print("headlessモードのため、ログイン画面を表示できません。スキップします。")
                notify_windows("⚠️ LETUSログインが必要です", "実提出チェックのため、check_letus_submissions.bat から一度ログインしてください。")
                context.close()
                return
            else:
                print("ブラウザ画面で理科大アカウントにログインしてください。")
                print("ログインが完了してカレンダー画面が表示されるまで待機します (最大180秒)...")
                try:
                    page.wait_for_url("**/calendar/**", timeout=180000)
                    print("✅ ログインが確認できました！セッションを保持しました。")
                    time.sleep(2)
                except Exception:
                    print("タイムアウトしました。処理を中断します。")
                    context.close()
                    return

        # Step 2: Extract event links from calendar page
        print("[ステップ 2] 画面内の課題リンクを収集しています (READ-ONLY)...")
        event_links = []
        for attempt in range(3):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
                event_links = page.evaluate("""() => {
                    const results = [];
                    const links = document.querySelectorAll('a[href*="/mod/assign/view.php"], a[href*="/mod/quiz/view.php"]');
                    links.forEach(a => {
                        const title = a.innerText.trim();
                        const href = a.href;
                        if (title && href && !results.some(r => r.href === href)) {
                            results.push({ title, href });
                        }
                    });
                    return results;
                }""")
                break
            except Exception as e:
                print(f"  イベント収集リトライ ({attempt+1}/3): {e}")
                time.sleep(2)

        print(f"  検出された課題リンク: {len(event_links)} 件")

        # 評価用ヘルパー
        check_submission_js = """() => {
            const pageText = document.body ? document.body.innerText : '';
            
            // Positive submission indicators
            const submittedElement = document.querySelector('.submissionstatussubmitted, .submissiongraded');
            const hasSubmittedText = 
                pageText.includes('評定のために提出済み') || 
                pageText.includes('Submitted for grading') ||
                (pageText.includes('完了') && pageText.includes('送信日時')) ||
                (pageText.includes('受験結果') && (pageText.includes('終了') || pageText.includes('送信')));

            // Explicit negative indicators (Unsubmitted)
            const hasUnsubmittedText = 
                pageText.includes('提出物はありません') || 
                pageText.includes('提出物なし') || 
                pageText.includes('ドラフト (未提出)') ||
                pageText.includes('未受験') ||
                pageText.includes('No attempt') ||
                pageText.includes('まだ受験していません');

            const statusCell = document.querySelector('td.submissionstatus, td.earlysubmission');
            const statusCellText = statusCell ? statusCell.innerText.trim() : '';

            let isSubmitted = false;
            if (submittedElement || hasSubmittedText) {
                isSubmitted = true;
            } else if (hasUnsubmittedText) {
                isSubmitted = false;
            } else if (statusCellText) {
                isSubmitted = statusCellText.includes('提出済み') || statusCellText.includes('Submitted');
            }

            return {
                isSubmitted: isSubmitted,
                hasSubmittedText: hasSubmittedText,
                hasUnsubmittedText: hasUnsubmittedText,
                statusText: statusCellText || (submittedElement ? submittedElement.innerText.trim() : '')
            };
        }"""

        # Step 3: Check unsubmitted assignments (未提出課題の実提出チェック)
        print("\n[ステップ 3] 未提出課題の実提出状況を確認中...")
        for a in unsubmitted_tasks:
            title = a.get("title", "")
            subject = a.get("subject", "")
            target_url = a.get("letusUrl")

            if not target_url:
                for ev in event_links:
                    clean_ev_title = re.sub(r'の提出期限|終了|第\d+回', '', ev["title"]).strip()
                    clean_a_title = re.sub(r'の提出期限|終了|第\d+回', '', title).strip()
                    if clean_a_title in ev["title"] or clean_ev_title in title or ev["title"] in title:
                        target_url = ev["href"]
                        a["letusUrl"] = target_url
                        break

            if not target_url:
                continue

            print(f"  確認中: 【{subject}】{title}")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1.2)
                status_info = page.evaluate(check_submission_js)

                if status_info.get("isSubmitted"):
                    print(f"    🟢 提出済み（緑マーク）を検知！ -> 自動完了に更新")
                    a["status"] = "submitted"
                    a["completedAt"] = datetime.now().isoformat()
                    updated_promoted.append(a)
                    notify_windows("✅ LETUS提出を確認", f"「{subject}: {title}」の提出を確認し、完了に更新しました。")
                else:
                    print(f"    ⚪ 未提出のままです")
            except Exception as e:
                print(f"    閲覧スキップ: {e}")

        # Step 4: Check accidental completion guard (誤完了セーフガード)
        print("\n[ステップ 4] 完了済み課題の誤完了セーフガード点検中...")
        for a in completed_check_candidates:
            title = a.get("title", "")
            subject = a.get("subject", "")
            target_url = a.get("letusUrl")

            if not target_url:
                for ev in event_links:
                    clean_ev_title = re.sub(r'の提出期限|終了|第\d+回', '', ev["title"]).strip()
                    clean_a_title = re.sub(r'の提出期限|終了|第\d+回', '', title).strip()
                    if clean_a_title in ev["title"] or clean_ev_title in title or ev["title"] in title:
                        target_url = ev["href"]
                        a["letusUrl"] = target_url
                        break

            if not target_url:
                continue

            print(f"  点検中: 【{subject}】{title}")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1.2)
                status_info = page.evaluate(check_submission_js)

                # もしLETUS側で確実に未提出 (提出物なし / 未受験) であれば誤完了！
                if status_info.get("hasUnsubmittedText") and not status_info.get("isSubmitted"):
                    print(f"    🚨【誤完了を検出】Webでは完了ですが、LETUSでは『未提出』です！")
                    a["status"] = "not_started"
                    a["completedAt"] = None
                    updated_reverted.append(a)
                    notify_windows(
                        "🚨【未提出の誤完了を検出】",
                        f"「{subject}: {title}」はLETUSで未提出のため、未完了に自動復元しました！"
                    )
                else:
                    print(f"    ✅ LETUS上でも提出済みを確認 (正常)")
            except Exception as e:
                print(f"    点検スキップ: {e}")

        print("\nブラウザを終了しています...")
        time.sleep(1)
        context.close()

    # Step 5: Save & Sync
    total_changes = len(updated_promoted) + len(updated_reverted)
    if total_changes > 0:
        save_assignments(assignments)
        print(f"\n🎉 合計 {total_changes} 件のステータスを更新しました！")
        if updated_promoted:
            print(f"  ・提出済みに昇格: {len(updated_promoted)} 件")
            for t in updated_promoted:
                sync_to_cloud(t["id"], "submitted", t.get("title", ""))
        if updated_reverted:
            print(f"  ・未完了に自動復元: {len(updated_reverted)} 件")
            for t in updated_reverted:
                sync_to_cloud(t["id"], "not_started", t.get("title", ""))

        summary = f"letus sync: {len(updated_promoted)} promoted, {len(updated_reverted)} reverted"
        sync_to_git(summary)
    else:
        print("\nステータスの変更はありませんでした（すべて整合しています）。")

    if morning_check_only:
        record_morning_scan_done()

    print("=" * 60)
    print("  スキャン完了！ダッシュボードと整合しました。")
    print("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LETUS提出状況 自動スキャナー＆セーフガード")
    parser.add_argument("--headless", action="store_true", help="ブラウザ画面を表示せずにバックグラウンドで実行")
    parser.add_argument("--morning-check-only", action="store_true", help="朝(06:00-12:00)かつ本日未実行の場合のみ実行するスマートモード")
    parser.add_argument("--force", action="store_true", help="朝判定をバイパスして強制実行")
    args = parser.parse_args()

    scan_letus_submissions(
        headless=args.headless,
        morning_check_only=args.morning_check_only,
        force=args.force
    )
