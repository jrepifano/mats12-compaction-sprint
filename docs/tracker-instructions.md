# How to update the MATS 12 experiment tracker

These instructions are for people and for Claude sessions. To give them to Claude, paste them into your project's CLAUDE.md, or into the chat.

## Rules

1. **A row exists only once the experiment has a plan doc** (a Claude Doc). Create the row when the plan doc is created. Ideas without a plan doc don't go in the tracker.
2. **Name each row with the plan doc's title**, and link the Plan column to that doc.
3. **Only edit your own rows.** Never fill in or infer anything about someone else's experiment. Their rows hold only what they wrote.
4. **Status is a dropdown with three values:**
   - **Designing:** the plan is being written, reviewed or revised, or the environment is being built. No paid runs yet.
   - **In progress:** the plan and budget are approved, and paid runs or analysis are under way.
   - **Done:** the report is written.
5. Keep the other columns current: Owner, Topic, "Now / next step" (one line), "Spend / cap" (actual dollars against the cap), and Updated (date of the change).
6. **Update your row at every status change, checkpoint and paid run.**

## For a Claude session editing the tracker with the Claude Docs tools

- Open the doc with the docs `read` tool, using the link above (the whole link is accepted as the id). Then read the tab's body in outline form to find the table.
- To add a row, insert a table row at the end of the table, spelling out every cell. For the Status cell, use a dropdown chip from the existing **"Stage"** enum.
- To change a cell, replace that cell (a table/row/column target), guarded with the revision you just read. If the write is refused because someone else edited the table, re-read and retry; never force.
- Don't touch other people's rows or the sections below the table.
