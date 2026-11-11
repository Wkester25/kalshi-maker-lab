# Daily review protocol

For whoever reviews the program each day, person or Claude. The aim is to learn something true from each
day's data without fooling ourselves, which is what went wrong before.

## Each day

1. Read `reports/latest.md`. If it is older than a day, the program is not running: say so and stop.
2. Check health first: problems logged, WebSocket book against REST, clock offset, recorded rows growing.
   A data problem makes the day's numbers unusable; fix that before anything else.
3. For each rule, read trades, mean, range, and filled against unfilled. Do not react to one day.
4. Propose **at most one change**, and only if the evidence since the last change supports it.
5. Write the change in `docs/CHANGES.md` (create it on first use): date, what changed, why, and what result
   on later data would count as success or failure.
6. Judge every change only on data recorded after it was made.

## Rules for changing rules

- A frozen rule's band, window or markets are not tuned on the data that scores it. If a rule needs changing,
  add a new rule with a new id and let both run.
- Switch a rule off when its mean is below zero after 2,500 trades, or its real-money result is down $6 per
  contract.
- Raise size only on cumulative results since launch: 2 contracts after 1,000 trades with the mean one
  standard error above zero, 3 after 2,500 trades and 1.5, 5 after 5,000 trades and 2. The scorecard prints
  the size the evidence supports.
- Real money for a rule comes after its paper range has stayed above zero on at least a week of data that
  did not choose its settings, and then at one contract.

## Things not to do

- Do not exclude a market because it lost. That was worth +0.18c of false edge in the old backtest.
- Do not compare a backtest with live results unless both use arrival timestamps.
- Do not add a filter because it separates winners from losers on one day.
