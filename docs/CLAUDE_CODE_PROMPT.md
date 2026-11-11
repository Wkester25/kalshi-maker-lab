# Prompt to start a Claude Code session on the server

Start Claude Code in the project folder (`cd ~/kalshi-maker-lab && claude`) and paste this:

---

You're picking up my Kalshi trading research project. I'm Whit, an EE student trading small size; I want the
truth about whether there's an edge, not reassurance. This program ("Kalshi Maker Lab") is running on this
server as the systemd service `kalshi-maker-lab`, recording Kalshi's 15-minute crypto and commodity markets and
paper-trading five frozen rules.

1. Read CLAUDE.md, then docs/HISTORY.md, docs/DATA.md, docs/REVIEW_LOOP.md and README.md. They explain what
   was learned so far, why the rules are frozen, and the mistakes not to repeat (look-ahead from a slow clock,
   tuning on the scoring data, dropping markets that lost).
2. Check the program is healthy: `systemctl status kalshi-maker-lab`, the last 100 lines of
   `journalctl -u kalshi-maker-lab`, and `.venv/bin/python tools/analyze.py`. In particular: is the WebSocket
   connected, has the WebSocket book ever disagreed with the REST book, is the clock offset reasonable, and
   are recordings growing in data/rec/?
3. Then give me a short report: health first, then each rule's results with ranges, filled vs unfilled bids,
   and the loss rate against break-even. Say plainly which questions the data can and can't answer yet, and
   how many more days each needs.
4. Propose at most one change, with the evidence for it and what result on later data would confirm it. Don't
   make it until I say so.

Do not switch live trading on, mark rules "Trade live", change sizes, or touch keys.json or secrets/. Read-only
analysis and running the tests are fine.

---

For a quick daily check, this shorter version works once the session knows the project:

> Run the daily review in docs/REVIEW_LOOP.md: health, tools/analyze.py, results by rule, at most one proposed
> change. Don't change anything without asking.
