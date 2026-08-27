# Reference screen

Put the operator's TT screenshot here as `tt_ladder.png`.

It is the layout specification for the ladder (§3.3 of
`MT5_TRADER_LADDER_PROMPT.md`): three inter-product ladders
(`CL Oct26 − BZ Oct26`, `CL Nov26 − BZ Nov26`, `Oct26 HO−CL Crack`), each with

- the title bar carrying pair name and routing account, and the bottom taskbar
  strip with a working-order count badge per tab;
- the quote strip: net change, `H` `L` `O` `V`;
- the left control rail: account, `Filter`, order type, TIF, armed quantity,
  the `1 / 1 5 / 10 50 / 100 CLR` keypad, default quantity, `CXL S` `CXL All`
  `CXL B` with their order-count superscripts, `Increment`;
- the grid: `Work | Bids | Price | Asks | LTQ`, a solid blue bid band and a
  solid red ask band, the thick black inside-market rule between them, the
  tinted last-trade price cell, and the `B: / W:` counts box.

The Playwright layout test reads this file's presence and asserts the shipped
UI against these metrics. Treat a difference as a failing test, not as taste.
