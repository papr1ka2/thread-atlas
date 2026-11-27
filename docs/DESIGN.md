# Thread Atlas design

Thread Atlas is a local app for people who keep work in several ChatGPT, Claude, and Codex threads. Ask “Where did we leave yesterday’s login bug?” to find the original thread and read its latest reply. Follow with “Then run the tests” to send the request back to the same conversation.

[Open the interactive prototype](./THREAD-ATLAS.html) · [Preview the work map](./THREAD-ATLAS.png) · [한국어 설계 문서](./DESIGN.ko.md)

**English is the default.** Choose **한국어** in the sidebar for the Korean interface and localized demo examples. Switching languages keeps the current project layout, hidden sessions, reply target, chat history, and draft. In the product, messages from real threads retain their original language. The prototype also preserves exactly what the user types.

## Find work and continue it

1. The user asks “Where did we leave yesterday’s login bug?” in the main chat.
2. The map highlights **Authentication › Token refresh bug**. If the project is collapsed, it expands while other projects keep their positions.
3. The main chat reports that the fix is in and Codex is waiting to run regression tests. The answer includes the latest source message, its timestamp, and a **View source** button. For an older snapshot, the product also shows when it last checked the thread.
4. The user types “Then run the tests.” The composer shows **Authentication › Token refresh bug · Codex** before sending. The follow-up belongs in that original conversation.
5. A reply arriving from another thread appears in the inbox. It does not take over the draft. **Reply here** explicitly changes its destination.

## Screen layout

| Area | User decision | Behavior |
| --- | --- | --- |
| Sidebar | What work should I see? | Open the work map, inbox, hidden sessions, or a project. The inbox count shows unread replies. |
| Work map | Which threads belong together? | A branch joins a project to its sessions. ChatGPT planning, Claude review, and Codex implementation can share a branch. Each card shows its provider, account or local scope, latest message, and status. |
| Main chat | Which task am I looking for, and where should my follow-up go? | A lookup highlights its source on the map. The composer shows its proposed destination before a send. |
| Inbox | What needs my attention now? | Completed replies and requests for input appear in arrival order. **View source** opens a message. **Reply here** addresses the composer to its thread. |

The product initially groups sessions using their source project or working directory. Unassigned work goes into an **Unsorted** group. Identity comes from the account, workspace, and original thread ID. Collapsing a project leaves its session, unread, and waiting-for-reply counts visible. New messages update cards in place so the user can return to a familiar map position.

## Drag and drop

| Drop a session on | Result | Alternative control |
| --- | --- | --- |
| Another project | Move it to that local group | Select a project in session details. |
| A card in the same group | Move the session before that card | The product should also support keyboard reordering. |
| The composer | Set the reply target and focus the draft | Select **Set as reply target** in session details. |
| Hidden | Remove the session from the map, notifications, and automatic routing candidates | Select **Hide session**. Use **Restore** to return it to its group. |

Hiding a session is reversible. The product should retain grouping and visibility across imports and reconnects. Permanent deletion from the original service is a separate, explicit operation.

## Lookup and routing

| Message | Expected behavior |
| --- | --- |
| “Where did we leave the login bug?” | Search the full local message history. Show the matching message and the latest source reply, each with its timestamp. |
| “Then run the tests.” | Continue the thread just found. Check whether it accepts replies and show the destination in the composer. |
| “Set payment retries to three.” | The newly named topic points to the payments thread, even when the previous search found the login bug. |
| “Ask Claude why the limit is three.” | Treat this as a request to send a question. Match the provider, account, and topic. Show the latest messages if the candidates are ambiguous. |
| “Go ahead.” | Continue the established target. If there is none, show candidate projects, providers, and last questions. Selecting a candidate confirms the send. |
| “Which threads need my reply?” | Collect waiting threads across sources. The user can select one to read and continue. |

Opening a source or receiving a notification leaves the draft attached to its current destination. A message that names a new task gets a fresh destination preview. For a read-only match, offer the source and **Copy draft**. Track successful delivery to the original thread separately from this manual handoff.

## Connections and notifications

For each connection, track session discovery, history access, new-reply detection, and delivery to an original thread independently. The product should expose the number of discovered, live-tracked, and directly writable sessions per account, plus the last successful sync. A closed tab or disconnected service keeps its last-known timestamp until it can be checked again.

| Source | Integration to evaluate | Verification before enabling direct replies |
| --- | --- | --- |
| Local Codex | Use App Server thread discovery, history, resume, and turn events. | Discover threads opened from desktop and CLI, capture replies started in another window, and confirm each follow-up appears exactly once under the same original ID. [Codex App Server](https://developers.openai.com/codex/app-server) |
| Local Claude Code | Read sessions by ID and use resume or hooks as appropriate. | Test a completed session and a session still running in another terminal separately. Establish which process owns input before writing to an active session. [Claude Code sessions](https://code.claude.com/docs/en/agent-sdk/sessions) · [Hooks](https://code.claude.com/docs/en/hooks) |
| Personal ChatGPT and Claude web chats | Import exported history and evaluate user-connected browser tabs for updates and direct replies. | Exports are point-in-time copies. A public API for subscribing to and replying to all existing personal web chats was not established in the design review. Test account switches, background tabs, closed tabs, and reconnects before advertising coverage. [ChatGPT exports](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data) · [Claude exports](https://support.claude.com/en/articles/9450526-export-your-claude-data) |

A completed answer or request for input should generate one inbox entry and one macOS notification. A menu-bar collector can continue receiving updates after the window closes. On wake, reconcile messages from the last successful sync. Optional phone notifications should let the user choose between project and thread names alone or a message excerpt.

## Implementation and acceptance criteria

Store map layout, source identifiers, evidence, read state, and outgoing requests in a local SQLite database. Route lookups to local search. Route outgoing requests through a queue keyed by draft and original thread ID. Distinguish local acceptance, acknowledgment by the source, and arrival of a reply. Reconcile an uncertain delivery with the source before retrying. A browser or terminal agent is a connector candidate if it can repeatedly establish account, thread, and delivery identity.

1. **Complete one end-to-end interaction.** Build a small map and main chat together. With two existing local sessions, demonstrate lookup, latest source, follow-up, and a reply in the same original thread. Test each personal web service separately, including its behavior after a tab closes.
2. **Measure routing.** Use 20 similarly named threads and 50 requests per supported language, without explicit titles or reply buttons. Proposed acceptance is at least 40 correct automatic sends per language and zero wrong-thread sends in that test. Evaluate ten ambiguous prompts, pure lookups, requests to ask another service, and drafts interrupted by notifications separately.
3. **Exercise the map at scale.** With 100 sessions, test collapse, lookup highlighting, drag and drop, hide and restore, and persistence after restart. Reconnect without flooding the inbox with historical notifications.
4. **Publish verified connector capabilities.** Mark a connection writable only after observing a round trip in the original thread. Publish code, docs, and synthetic fixtures. Keep real conversation data and credentials on the local machine.

## Prototype scope

The offline HTML prototype includes three fictional projects and six sessions. It demonstrates source lookup, candidate selection, follow-ups, a simulated incoming reply, read-only handoff, dragging, hiding, and restoration. Reloading the page resets demo data. The bare prototype URL opens in English, and the Korean deep link opens with Korean selected. Live accounts, production routing accuracy, original-thread delivery, and operating-system or phone notifications require implementation testing.
