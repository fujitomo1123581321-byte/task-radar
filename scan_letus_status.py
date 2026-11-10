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
ACTIVE_COURSES = [
    {"name": "データサイエンス・ＡＩ応用基礎", "id": 206066},
    {"name": "プラズマ物理", "id": 206316},
    {"name": "物理学実験３B組", "id": 204216},
    {"name": "素粒子・原子核物理学", "id": 206296},
    {"name": "宇宙の物理学１", "id": 206297},
    {"name": "物理数学演習２Ｂ", "id": 206300},
    {"name": "物理数学演習２Ａ", "id": 206299},
    {"name": "量子力学３", "id": 206309},
    {"name": "統計力学２", "id": 206314},
    {"name": "数値計算３", "id": 206320},
    {"name": "国際関係論", "id": 211878},
    {"name": "宇宙科学総合", "id": 173828}
]

# Prefer local SSD path (%LOCALAPPDATA%\TaskRadar\letus_browser_profile) to avoid Box Drive sync locks
LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA")
if LOCAL_APP_DATA:
    BROWSER_PROFILE_DIR = os.path.join(LOCAL_APP_DATA, "TaskRadar", "letus_browser_profile")
    OLD_PROFILE_DIR = os.path.join(BASE_DIR, ".letus_browser_profile")
    if os.path.exists(OLD_PROFILE_DIR) and not os.path.exists(BROWSER_PROFILE_DIR):
        try:
            import shutil
            shutil.copytree(OLD_PROFILE_DIR, BROWSER_PROFILE_DIR)
        except Exception:
            pass
else:
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

def sync_to_cloud(task_id, status=None, title="", letus_url=None):
    """Render APIにステータスやletusUrlの変更を即座にPOST通知"""
    try:
        data = {"id": task_id, "title": title}
        if status:
            data["status"] = status
        if letus_url:
            data["letusUrl"] = letus_url
        payload = json.dumps(data).encode("utf-8")
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
    urls_updated = []      # LETUS固有URLを新規取得・保存

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
            page.goto("https://letus.ed.tus.ac.jp/calendar/view.php?view=upcoming", wait_until="commit", timeout=30000)
            try:
                page.wait_for_url("**/calendar/view.php*", timeout=20000)
            except Exception:
                pass
            page.wait_for_load_state("domcontentloaded")
            time.sleep(2)
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
                print("ログイン完了を待機しています (最大180秒)...")
                try:
                    logged_in = False
                    start_t = time.time()
                    while time.time() - start_t < 180:
                        cur_url = page.url
                        if "letus.ed.tus.ac.jp" in cur_url and "idp.admin.tus.ac.jp" not in cur_url and "saml" not in cur_url.lower():
                            logged_in = True
                            break
                        time.sleep(1)

                    if not logged_in:
                        print("タイムアウトしました。処理を中断します。")
                        context.close()
                        return

                    print("✅ ログインが確認できました！セッションを保持しました。")
                    if "calendar" not in page.url:
                        page.goto("https://letus.ed.tus.ac.jp/calendar/view.php?view=upcoming", wait_until="domcontentloaded", timeout=20000)
                    time.sleep(2)
                except Exception as e:
                    print(f"ログイン待機エラー: {e}")
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
                    const cards = document.querySelectorAll('.event, [data-event-id], .calendar_event_course, .card');
                    cards.forEach(card => {
                        const titleEl = card.querySelector('.name, h3, .card-title');
                        const linkEl = card.querySelector('a[href*="/mod/assign/view.php"], a[href*="/mod/quiz/view.php"], a[href*="/mod/feedback/view.php"], a[href*="/mod/questionnaire/view.php"], a[href*="/mod/forum/view.php"]');
                        const courseEl = card.querySelector('.course, .text-muted, a[href*="/course/view.php"]');
                        if (linkEl) {
                            const title = titleEl ? titleEl.innerText.trim() : linkEl.innerText.trim();
                            const rawHref = linkEl.href || '';
                            const href = rawHref.replace(/([?&])action=editsubmission(&|$)/, '$1').replace(/[?&]$/, '');
                            const course = courseEl ? courseEl.innerText.trim() : '';
                            if (href && !results.some(r => r.href === href)) {
                                results.push({ title, href, course });
                            }
                        }
                    });
                    if (results.length === 0) {
                        const links = document.querySelectorAll('a[href*="/mod/assign/view.php"], a[href*="/mod/quiz/view.php"], a[href*="/mod/feedback/view.php"], a[href*="/mod/questionnaire/view.php"], a[href*="/mod/forum/view.php"]');
                        links.forEach(a => {
                            const title = a.innerText.trim();
                            const rawHref = a.href || '';
                            const href = rawHref.replace(/([?&])action=editsubmission(&|$)/, '$1').replace(/[?&]$/, '');
                            if (title && href && !results.some(r => r.href === href)) {
                                results.push({ title, href, course: '' });
                            }
                        });
                    }
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
                (pageText.includes('受験結果') && (pageText.includes('終了') || pageText.includes('送信'))) ||
                pageText.includes('回答を送信しました') ||
                pageText.includes('すでに回答しています') ||
                pageText.includes('回答済み') ||
                pageText.includes('あなたの回答は記録されました');

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
                    ev_title = ev.get("title", "")
                    ev_course = ev.get("course", "")
                    
                    # Clean brackets and suffixes
                    c_ev = re.sub(r'[「」『』()（）\[\]【】\s・:：]', '', ev_title).lower()
                    c_a = re.sub(r'[「」『』()（）\[\]【】\s・:：]', '', title).lower()
                    
                    # Must match course keyword if available
                    if ev_course and subject:
                        # Simple keyword overlap
                        c_subj = re.sub(r'[「」『』()（）\[\]【】\s・:：\d]', '', subject).lower()
                        c_evc = re.sub(r'[「」『』()（）\[\]【】\s・:：\d]', '', ev_course).lower()
                        if c_subj and c_evc and not any(k in c_evc for k in c_subj[:3]):
                            continue
                            
                    # Number check: if title has numbers (e.g. 5, 8, 11/8), ev_title must match
                    nums_a = re.findall(r'\d+', title)
                    nums_ev = re.findall(r'\d+', ev_title)
                    if nums_a and nums_ev:
                        if not any(n in nums_ev for n in nums_a):
                            continue
                    elif nums_a and not nums_ev:
                        continue

                    # Substring check
                    clean_ev_stripped = re.sub(r'の提出期限|の受験可能期間の終了|終了|締切|課題|小テスト', '', c_ev)
                    clean_a_stripped = re.sub(r'の提出期限|の受験可能期間の終了|終了|締切|課題|小テスト', '', c_a)
                    if clean_a_stripped and (clean_a_stripped in clean_ev_stripped or clean_ev_stripped in clean_a_stripped):
                        target_url = ev["href"]
                        a["letusUrl"] = target_url
                        if a not in urls_updated:
                            urls_updated.append(a)
                        break

            # Fallback: sweep course page if calendar didn't have this task
            if not target_url and ACTIVE_COURSES:
                matched_course = None
                for c in ACTIVE_COURSES:
                    c_name_norm = re.sub(r'[\s\d・()（）]', '', c["name"]).lower()
                    subj_norm = re.sub(r'[\s\d・()（）]', '', subject).lower()
                    if (len(subj_norm) >= 2 and subj_norm[:3] in c_name_norm) or (len(c_name_norm) >= 2 and c_name_norm[:3] in subj_norm):
                        matched_course = c
                        break

                if matched_course:
                    course_url = f"https://letus.ed.tus.ac.jp/course/view.php?id={matched_course['id']}"
                    try:
                        print(f"  カレンダー未掲載のためコースから探索中: 【{matched_course['name']}】({course_url})")
                        page.goto(course_url, wait_until="domcontentloaded", timeout=20000)
                        time.sleep(1.0)
                        course_links = page.evaluate("""() => {
                            const res = [];
                            document.querySelectorAll('a[href*="/mod/assign/view.php"], a[href*="/mod/quiz/view.php"], a[href*="/mod/feedback/view.php"], a[href*="/mod/questionnaire/view.php"], a[href*="/mod/forum/view.php"]').forEach(a => {
                                const title = a.innerText.trim();
                                const rawHref = a.href || '';
                                const href = rawHref.replace(/([?&])action=editsubmission(&|$)/, '$1').replace(/[?&]$/, '');
                                if (title && href && !res.some(r => r.href === href)) {
                                    res.push({ title, href });
                                }
                            });
                            return res;
                        }""")
                        c_a = re.sub(r'[「」『』()（）\[\]【】\s・:：]', '', title).lower()
                        nums_a = re.findall(r'\d+', title)
                        for cl in course_links:
                            cl_title = cl.get("title", "")
                            c_cl = re.sub(r'[「」『』()（）\[\]【】\s・:：]', '', cl_title).lower()
                            nums_cl = re.findall(r'\d+', cl_title)
                            if nums_a and nums_cl:
                                if not any(n in nums_cl for n in nums_a):
                                    continue
                            elif nums_a and not nums_cl:
                                continue

                            clean_cl_stripped = re.sub(r'の提出期限|の受験可能期間の終了|終了|締切|課題|小テスト', '', c_cl)
                            clean_a_stripped = re.sub(r'の提出期限|の受験可能期間の終了|終了|締切|課題|小テスト', '', c_a)
                            if clean_a_stripped and (clean_a_stripped in clean_cl_stripped or clean_cl_stripped in clean_a_stripped):
                                target_url = cl["href"]
                                a["letusUrl"] = target_url
                                if a not in urls_updated:
                                    urls_updated.append(a)
                                print(f"    ✨ コースページからリンクを取得成功: {target_url}")
                                break
                    except Exception as e:
                        print(f"    コース探索エラー: {e}")

            if not target_url:
                continue

            print(f"  確認中: 【{subject}】{title}")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1.2)
                status_info = page.evaluate(check_submission_js)

                if status_info.get("isSubmitted"):
                    print(f"    🟢 LETUS上で提出済み（緑マーク）を検知！")
                    if not a.get("letusSubmitted"):
                        a["letusSubmitted"] = True
                        if a not in urls_updated:
                            urls_updated.append(a)
                    notify_windows("🟢 LETUS提出を確認", f"「{subject}: {title}」のLETUS提出を確認しました（手動で完了できます）。")
                else:
                    print(f"    ⚪ LETUS上では未提出です")
                    if a.get("letusSubmitted"):
                        a["letusSubmitted"] = False
                        if a not in urls_updated:
                            urls_updated.append(a)
            except Exception as e:
                print(f"    閲覧スキップ: {e}")

        # Step 4: Check accidental completion guard (通知のみ・手動主導)
        print("\n[ステップ 4] 完了済み課題のLETUS提出確認中 (確認・通知のみ)...")
        for a in completed_check_candidates:
            title = a.get("title", "")
            subject = a.get("subject", "")
            target_url = a.get("letusUrl")

            if not target_url:
                for ev in event_links:
                    ev_title = ev.get("title", "")
                    ev_course = ev.get("course", "")
                    
                    # Clean brackets and suffixes
                    c_ev = re.sub(r'[「」『』()（）\[\]【】\s・:：]', '', ev_title).lower()
                    c_a = re.sub(r'[「」『』()（）\[\]【】\s・:：]', '', title).lower()
                    
                    # Must match course keyword if available
                    if ev_course and subject:
                        # Simple keyword overlap
                        c_subj = re.sub(r'[「」『』()（）\[\]【】\s・:：\d]', '', subject).lower()
                        c_evc = re.sub(r'[「」『』()（）\[\]【】\s・:：\d]', '', ev_course).lower()
                        if c_subj and c_evc and not any(k in c_evc for k in c_subj[:3]):
                            continue
                            
                    # Number check: if title has numbers (e.g. 5, 8, 11/8), ev_title must match
                    nums_a = re.findall(r'\d+', title)
                    nums_ev = re.findall(r'\d+', ev_title)
                    if nums_a and nums_ev:
                        if not any(n in nums_ev for n in nums_a):
                            continue
                    elif nums_a and not nums_ev:
                        continue

                    # Substring check
                    clean_ev_stripped = re.sub(r'の提出期限|の受験可能期間の終了|終了|締切|課題|小テスト', '', c_ev)
                    clean_a_stripped = re.sub(r'の提出期限|の受験可能期間の終了|終了|締切|課題|小テスト', '', c_a)
                    if clean_a_stripped and (clean_a_stripped in clean_ev_stripped or clean_ev_stripped in clean_a_stripped):
                        target_url = ev["href"]
                        a["letusUrl"] = target_url
                        if a not in urls_updated:
                            urls_updated.append(a)
                        break

            if not target_url:
                continue

            print(f"  点検中: 【{subject}】{title}")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1.2)
                status_info = page.evaluate(check_submission_js)

                if status_info.get("hasUnsubmittedText") and not status_info.get("isSubmitted"):
                    print(f"    ⚠️【要確認】Webでは完了ですが、LETUS側では未提出です")
                    notify_windows(
                        "⚠️【LETUS提出の確認】",
                        f"「{subject}: {title}」は完了になっていますが、LETUSでは未提出の可能性があります。リンクからご確認ください。"
                    )
                else:
                    print(f"    ✅ LETUS上でも提出済みを確認 (正常)")
            except Exception as e:
                print(f"    点検スキップ: {e}")

        print("\nブラウザを終了しています...")
        time.sleep(1)
        context.close()

    # Step 5: Save & Sync
    total_changes = len(updated_promoted) + len(updated_reverted) + len(urls_updated)
    if total_changes > 0:
        save_assignments(assignments)
        print(f"\n🎉 合計 {total_changes} 件の変更を反映しました！")
        if updated_promoted:
            print(f"  ・提出済みに昇格: {len(updated_promoted)} 件")
            for t in updated_promoted:
                sync_to_cloud(t["id"], status="submitted", title=t.get("title", ""), letus_url=t.get("letusUrl"))
        if updated_reverted:
            print(f"  ・未完了に自動復元: {len(updated_reverted)} 件")
            for t in updated_reverted:
                sync_to_cloud(t["id"], status="not_started", title=t.get("title", ""))
        if urls_updated:
            print(f"  ・LETUS固有URLを記録: {len(urls_updated)} 件")
            for t in urls_updated:
                if t not in updated_promoted:
                    sync_to_cloud(t["id"], title=t.get("title", ""), letus_url=t.get("letusUrl"))

        summary = f"letus sync: {len(updated_promoted)} promoted, {len(updated_reverted)} reverted, {len(urls_updated)} URLs saved"
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
