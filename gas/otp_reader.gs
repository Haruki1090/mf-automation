/**
 * MoneyForward ID OTP 自動取得スクリプト
 *
 * デプロイ手順:
 *   1. Google Apps Script (script.google.com) で新規プロジェクト作成
 *   2. このコードを貼り付け、SECRET を任意の文字列に変更
 *   3. 「デプロイ」→「新しいデプロイ」→ 種類: ウェブアプリ
 *      - 次のユーザーとして実行: 自分
 *      - アクセスできるユーザー: 全員
 *   4. デプロイURLを .env の GAS_OTP_URL に設定
 *   5. SECRET の値を .env の GAS_OTP_SECRET に設定
 *
 * 動作確認:
 *   curl "https://script.google.com/macros/s/YOUR_ID/exec?secret=YOUR_SECRET"
 */

const SECRET = "mf-otp-secret-2026";

function doGet(e) {
  // 認証チェック
  if (!e || e.parameter.secret !== SECRET) {
    return jsonResponse({ error: "unauthorized" });
  }

  // 直近10分以内の MF OTPメールを検索
  const query = 'from:do_not_reply@moneyforward.com subject:"マネーフォワード ID メールによる追加認証" newer_than:10m';
  const threads = GmailApp.search(query, 0, 1);

  if (threads.length === 0) {
    return jsonResponse({ code: null, message: "no email found" });
  }

  // 最新メッセージ本文から6桁コードを抽出
  const messages = threads[0].getMessages();
  const latest = messages[messages.length - 1];
  const body = latest.getPlainBody();

  const match = body.match(/\b([0-9]{6})\b/);
  const code = match ? match[1] : null;

  // 取得したら既読にしておく（次回の誤取得防止）
  if (code) {
    latest.markRead();
  }

  return jsonResponse({ code: code });
}

// デバッグ用: GASエディタから直接実行してメール検索状況を確認
function debugSearch() {
  const query = 'from:do_not_reply@moneyforward.com subject:"マネーフォワード ID メールによる追加認証"';
  const threads = GmailApp.search(query, 0, 5);
  console.log("検索結果スレッド数:", threads.length);
  threads.forEach((t, i) => {
    const msg = t.getMessages()[0];
    console.log(`[${i}] subject="${msg.getSubject()}" from="${msg.getFrom()}" date=${msg.getDate()}`);
  });
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
