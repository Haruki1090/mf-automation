import { Worker } from "@notionhq/workers";
import { j } from "@notionhq/workers/schema-builder";

const worker = new Worker();
export default worker;

// ---------------------------------------------------------------------------
// 型定義
// ---------------------------------------------------------------------------

type ScrapeMode =
  | "current_month"
  | "last_month"
  | "this_week"
  | "last_week"
  | "month"
  | "range";

// optional フィールドは null 許容にして JSONValue 制約を満たす
interface TriggerInput {
  mode: ScrapeMode;
  month: string | null;
  from_date: string | null;
  to_date: string | null;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// ツール: MF 家計簿データ取得
// Notion カスタムエージェントからツールとして呼び出される
// ---------------------------------------------------------------------------

worker.tool("mf-trigger-scraping", {
  title: "MF家計簿データ取得",
  description:
    "MoneyForward ME から入出金明細・口座残高を取得して Notion の家計簿_取引履歴 DB に保存します。" +
    "処理は非同期（30〜90秒）で実行されます。完了後はジョブ実行履歴 DB でステータスを確認してください。",
  schema: j.object({
    mode: j
      .enum(
        "current_month",
        "last_month",
        "this_week",
        "last_week",
        "month",
        "range"
      )
      .describe(
        "取得期間モード。current_month=今月, last_month=先月, this_week=今週, last_week=先週, month=指定月, range=任意期間"
      ),
    month: j
      .string()
      .nullable()
      .describe("指定月 YYYY-MM 形式。mode=month のときのみ使用。不要な場合は null"),
    from_date: j
      .string()
      .nullable()
      .describe("開始日 YYYY-MM-DD 形式。mode=range のときのみ使用。不要な場合は null"),
    to_date: j
      .string()
      .nullable()
      .describe("終了日 YYYY-MM-DD 形式。mode=range のときのみ使用。不要な場合は null"),
  }),

  execute: async (rawInput, { notion }) => {
    const input = rawInput as TriggerInput;
    const validationError = validateTriggerInput(input);
    if (validationError) {
      return {
        status: "error",
        job_page_id: "",
        message: validationError,
      };
    }

    const jobDbId = process.env.MF_JOB_DB_ID;
    const ghPat = process.env.GH_PAT;
    const ghRepo = process.env.GH_REPO ?? "Haruki1090/mf-automation";

    if (!jobDbId || !ghPat) {
      return {
        status: "error",
        job_page_id: "",
        message: "環境変数 MF_JOB_DB_ID または GH_PAT が未設定です",
      };
    }

    const now = new Date().toISOString();
    const label = modeLabel(input);

    // ── 1. ジョブページを作成（状態: 実行中）───────────────────────────
    let jobPage: { id: string };
    try {
      jobPage = await notion.pages.create({
        parent: { database_id: jobDbId },
        properties: {
          名前: { title: [{ text: { content: `MFスクレイピング ${now.slice(0, 10)} [${input.mode}]` } }] },
          状態: { select: { name: "実行中" } },
          実行モード: { select: { name: input.mode } },
          取得期間: { rich_text: [{ text: { content: label } }] },
          開始日時: { date: { start: now } },
        },
      }) as { id: string };
    } catch (err) {
      return {
        status: "error",
        job_page_id: "",
        message: `ジョブページの作成に失敗しました: ${String(err)}`,
      };
    }

    const jobPageId = jobPage.id;

    // ── 2. GitHub Actions repository_dispatch ──────────────────────────
    const dispatchRes = await fetch(
      `https://api.github.com/repos/${ghRepo}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${ghPat}`,
          "Content-Type": "application/json",
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({
          event_type: "scrape-moneyforward",
          client_payload: {
            mode: input.mode,
            month: input.month ?? "",
            from_date: input.from_date ?? "",
            to_date: input.to_date ?? "",
            job_page_id: jobPageId,
          },
        }),
      }
    );

    if (!dispatchRes.ok) {
      const errText = await dispatchRes.text();
      await notion.pages.update({
        page_id: jobPageId,
        properties: {
          状態: { select: { name: "エラー" } },
          エラー詳細: {
            rich_text: [
              { text: { content: `GitHub API error ${dispatchRes.status}: ${errText}` } },
            ],
          },
        },
      });
      return {
        status: "error",
        job_page_id: jobPageId,
        message: `GitHub Actions の起動に失敗しました (HTTP ${dispatchRes.status})`,
      };
    }

    // ── 3. 成功レスポンス ───────────────────────────────────────────────
    return {
      status: "started",
      job_page_id: jobPageId,
      message:
        `スクレイピングを開始しました（${label}）。` +
        `ジョブ実行履歴 DB でステータスを確認できます。job_page_id: ${jobPageId}`,
    };
  },
});

// ---------------------------------------------------------------------------
// ヘルパー
// ---------------------------------------------------------------------------

function modeLabel(input: TriggerInput): string {
  switch (input.mode) {
    case "current_month": return "今月";
    case "last_month":    return "先月";
    case "this_week":     return "今週";
    case "last_week":     return "先週";
    case "month":         return `指定月: ${input.month ?? ""}`;
    case "range":         return `${input.from_date ?? ""} 〜 ${input.to_date ?? ""}`;
  }
}

function validateTriggerInput(input: TriggerInput): string | null {
  if (input.mode === "month") {
    if (!input.month) {
      return "mode=month では month を YYYY-MM 形式で指定してください";
    }
    if (!/^\d{4}-\d{2}$/.test(input.month)) {
      return "month は YYYY-MM 形式で指定してください";
    }
    const month = Number(input.month.slice(5, 7));
    if (month < 1 || month > 12) {
      return "month の月は 01〜12 の範囲で指定してください";
    }
  }

  if (input.mode === "range") {
    if (!input.from_date || !input.to_date) {
      return "mode=range では from_date と to_date を YYYY-MM-DD 形式で指定してください";
    }
    const fromDate = parseIsoDate(input.from_date);
    const toDate = parseIsoDate(input.to_date);
    if (!fromDate || !toDate) {
      return "from_date と to_date は YYYY-MM-DD 形式で指定してください";
    }
    if (fromDate.getTime() > toDate.getTime()) {
      return "from_date は to_date 以前の日付を指定してください";
    }
  }

  return null;
}

function parseIsoDate(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const parsed = new Date(Date.UTC(year, month - 1, day));

  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    return null;
  }

  return parsed;
}
