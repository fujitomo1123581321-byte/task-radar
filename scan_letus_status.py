"""
LETUS 提出状況 自動スキャナー (完全READ-ONLY)
- LETUSの課題ページを開き、「緑色 (評定のために提出済み)」を読み取って自動判定
- LETUS側のボタン操作・フォーム送信・データ変更は一切行いません (閲覧のみ)
- 判定結果を assignments.json と assignments.js に反映
"""
import os
import sys
import json
import re
import time
from datetime import datetime
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE_DIR, "assignments.json")
JS_PATH = os.path.join(BASE_DIR, "assignments.js")
BROWSER_PROFILE_DIR = os.path.join(BASE_DIR, ".letus_browser_profile")

# Console UTF-8 fix
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
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

def scan_letus_submissions():
    print("=" * 60)
    print("  🔍 LETUS提出状況 自動スキャナー (完全READ-ONLY)")
    print("  ※LETUSへの変更・送信操作は一切行いません（閲覧のみ）")
    print("=" * 60)

    assignments = load_assignments()
    if not assignments:
        print("課題データが存在しません。")
        return

    # Filter unsubmitted academic assignments
    active_assignments = [a for a in assignments if a.get("category") == "academic" and a.get("status") != "submitted"]
    print(f"現在、未提出の学業課題: {len(active_assignments)} 件\n")

    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)

    with sync_playwright() as p:
        # Launch persistent browser so login session is preserved
        print("ブラウザを起動しています (Edge)...")
        context = p.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            channel="msedge",
            headless=False, # Visible so user can see it only reads, and can login if needed
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"]
        )

        page = context.new_page()

        # Step 1: Open upcoming events calendar
        print("\n[ステップ 1] LETUSの今後のイベント画面を開いています...")
        page.goto("https://letus.ed.tus.ac.jp/calendar/view.php?view=upcoming", wait_until="domcontentloaded")
        time.sleep(2)

        # Check if login is needed
        if "login" in page.url.lower() or "auth" in page.url.lower():
            print("\n⚠️ 【初回ログインが必要です】")
            print("ブラウザ画面で理科大アカウントにログインしてください。")
            print("ログインが完了してカレンダー画面が表示されるまで待機します...")
            try:
                page.wait_for_url("**/calendar/**", timeout=120000)
                print("✅ ログインが確認できました！セッションを保存しました。")
            except Exception:
                print("タイムアウトしました。処理を中断します。")
                context.close()
                return

        time.sleep(2)

        # Step 2: Extract event links from calendar page
        print("[ステップ 2] 画面内の課題リンクを収集しています (READ-ONLY)...")
        event_links = page.evaluate("""() => {
            const results = [];
            // Find all event links in Moodle calendar
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

        print(f"  検出された課題リンク: {len(event_links)} 件")

        # Step 3: Check each active assignment
        updated_count = 0

        for a in active_assignments:
            title = a.get("title", "")
            subject = a.get("subject", "")
            target_url = a.get("letusUrl")

            # Try to match with event_links if letusUrl not yet stored
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

            print(f"\n確認中: 【{subject}】{title}")
            print(f"  URL (GET閲覧): {target_url}")

            try:
                # Strictly GET navigation (READ-ONLY)
                page.goto(target_url, wait_until="domcontentloaded")
                time.sleep(1.5)

                # Check submission status
                status_info = page.evaluate("""() => {
                    // Check Moodle assign submission status
                    const submittedElement = document.querySelector('.submissionstatussubmitted, .submissiongraded');
                    const statusCell = document.querySelector('td.submissionstatus, td.earlysubmission');
                    
                    // Check quiz status
                    const quizStatus = document.querySelector('.quizattemptsummary, .generaltable');
                    
                    const pageText = document.body.innerText;
                    
                    const isSubmitted = 
                        !!submittedElement || 
                        pageText.includes('評定のために提出済み') || 
                        pageText.includes('Submitted for grading') ||
                        (pageText.includes('完了') && pageText.includes('送信日時'));

                    return {
                        isSubmitted: isSubmitted,
                        statusText: statusCell ? statusCell.innerText.trim() : (submittedElement ? submittedElement.innerText.trim() : '')
                    };
                }""")

                if status_info.get("isSubmitted"):
                    print(f"  🟢 提出済み（緑色マーク）を検知しました！")
                    a["status"] = "submitted"
                    a["completedAt"] = datetime.now().isoformat()
                    updated_count += 1
                else:
                    print(f"  ⚪ 未提出（提出物なし）のままです")

            except Exception as e:
                print(f"  スキップ (閲覧エラー): {e}")

        # Save results
        if updated_count > 0:
            save_assignments(assignments)
            print(f"\n🎉 合計 {updated_count} 件の課題を「提出完了」に更新しました！")
        else:
            print("\n新たに提出完了になった課題はありませんでした。")

        print("\nブラウザを終了しています...")
        time.sleep(1)
        context.close()

    print("=" * 60)
    print("  スキャン完了！ダッシュボードに反映されました。")
    print("=" * 60)

if __name__ == "__main__":
    scan_letus_submissions()
