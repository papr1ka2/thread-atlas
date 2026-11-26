# Thread Atlas

Find the AI thread you were working in, inspect its latest reply, and send a follow-up to that original conversation. Thread Atlas runs on your computer and groups Codex, Claude Code, ChatGPT, and Claude conversations by project. English is the default. The sidebar also supports Korean.

![Thread Atlas running against synthetic browser fixtures](docs/dashboard.png)

## Run locally

On macOS, double-click **Start.command** or run it in a terminal. It starts a loopback server, opens the authenticated dashboard, and uses the locked Python dependencies when uv is installed. Python 3.9+ is required for the fallback installer. The originals of your conversations stay in their source applications.

```sh
./Start.command
```

The [v0.1.0 release](https://github.com/ainta/thread-atlas/releases/tag/v0.1.0) includes a locally built Apple Silicon menu-bar app. It uses the same Python runtime and is ad-hoc signed. For a native build on your own Mac, build and open the bundle. Building requires the Xcode command line tools. The menu can reopen the dashboard or reveal the extension folder. The collector continues running after you close the dashboard.

```sh
./macos/build.sh
open "build/Thread Atlas.app"
```

On Linux, run `uv sync --frozen` followed by `uv run python -m atlas`. Use `--no-open` for a headless collector. To open an existing dashboard, run the launcher again. Its private launch URL is stored in the app data directory.

## Connect your conversations

| Source | Discover and read | Send to the original |
| --- | --- | --- |
| Codex | Incrementally indexes user-facing messages from local session transcripts. | Uses the signed-in local App Server daemon over its Unix socket. A busy thread must finish its current turn. |
| Claude Code | Indexes local project transcripts and watches for new replies. | Resumes the exact session ID in its original working directory. A session owned by another terminal is protected from concurrent writes. |
| ChatGPT web | Import conversations JSON or ZIP exports. The Chrome extension tracks selected open conversations. | Available in a connected tab when **Allow replies to this thread** is enabled and the page exposes a usable composer. |
| Claude web | Import Claude chat exports or track open conversations through the extension. | Uses the same opt-in tab mechanism, with a source-specific message reader. |

For Codex, keep the local App Server daemon running. On versions with daemon management, use `codex app-server daemon start` if it is stopped. Claude Code requires a signed-in CLI with `claude agents --json` support. Source visibility depends on the local profile and retained transcripts.

**For web history**, open **Connections**, pick a provider and account label, and select an exported JSON or ZIP. Re-importing a known message does not create a second copy. ChatGPT imports use the current conversation branch when supplied in the export.

**For live web tabs**, open `chrome://extensions`, enable Developer mode, click **Load unpacked**, and select this repository’s `extension` folder. Reload an existing source tab after installing. In the dashboard’s **Connections**, copy the pairing key. Open the extension on a ChatGPT or Claude conversation, paste the key, and use the same label as your imports. **Track open chats** pairs currently open conversations for reading. To send, enable replies on the individual tab. A full page reload, a changed conversation URL, or a disconnected tab can require reconnecting it. An export is a snapshot. Closed web chats do not receive live monitoring through this extension.

## Use the workspace

- Drag a card into a project, onto **Hidden**, or onto the composer to pin a reply target. Use card details for equivalent move and hide controls.
- Ask **“Where did we leave the payment retry policy?”** to retrieve a source message. Lookups search every stored message. The answer shows the matching message with its time, next to the latest reply when they differ, and **View source** opens the history at that message. Type **“Then run the tests”** to continue it. The destination appears before you send. Ambiguous requests offer candidates. **Find** and **Send to thread** let you explicitly select an intent.
- **Inbox** shows latest replies. **Reply here** pins the thread, while simply opening a source shows its history. New replies do not redirect a draft.
- Enable **Local notifications** under Connections to receive macOS notifications while the collector runs. Allow **Thread Atlas Alerts** when macOS asks. Clicking a banner opens that specific reply in the local inbox. The release bundles the notification helper. Running from source on macOS builds it once using the Xcode command line tools. Optional Telegram notifications use the environment variables `ATLAS_TELEGRAM_TOKEN` and `ATLAS_TELEGRAM_CHAT` supplied at launch. Only the thread title is sent. Delivery to Telegram needs a configured bot and recipient.
- Delivery receipts distinguish queued, sending, delivered, answered, failed, and uncertain. After an uncertain send, inspect the original thread before submitting a new draft.

## Data and configuration

The server binds to `127.0.0.1:43821`. Its private SQLite database, pairing credential, and launch URL live in `~/Library/Application Support/Thread Atlas` on macOS and under `$XDG_DATA_HOME/Thread Atlas` on Linux. Use `--port`, `--data`, `--codex-dir`, or `--claude-dir` to override the defaults. The map layout and read positions persist in SQLite. Draft text, language, reply target, and the main-chat search history persist in the dashboard's browser storage. Use separate browser profiles and account labels to distinguish identities.

The web extension reads visible message elements, communicates with the paired loopback service, and never needs account cookies or API keys. DOM controls can change with provider updates. The extension refuses to overwrite a nonempty composer or treat a button click alone as proof of delivery.

## Verification

[Verification report](docs/VERIFICATION.md) · [Original-thread receipts](docs/live-receipts.json) · [English design](docs/DESIGN.md) · [한국어 설계](docs/DESIGN.ko.md)

```sh
uv sync --frozen
uv run python -m unittest discover -s test -v
node test/router.mjs
uv pip install playwright
uv run playwright install chromium
uv run python scripts/browser_test.py
```

The browser test launches an isolated Chromium profile, loads the actual extension, and uses local fixtures in place of the two provider websites. Name full-history steps as arguments, for example `uv run python scripts/browser_test.py older_match_choices`, to run only those after the shared checks. An additional opt-in test uses your authenticated models, creates four disposable conversations, and consumes model usage.

```sh
uv run python scripts/live_test.py --live
```
