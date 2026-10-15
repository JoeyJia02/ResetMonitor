# ResetMonitor entry-point guidance

The installed background entry is `background_monitor.py --serve`; the desktop
test calls `manual_test.py`. The legacy `main.py --live` validation entry and
`*_candidate.py` files are separate paths. Review evidence must identify which
entry was tested. Never perform paid API or SMTP calls as an offline test.

## Code Review Rules

### Paid calls and concurrency

- Preserve the per-round ceiling of one collection returning at most three tweets,
  one batch classification request and one notification attempt. Flag changes that
  fetch a larger dataset before slicing, add hidden retries, or clear an uncertain
  attempt to run it again. Retain durable round claims and locking before external
  side effects. Any intentional change to this policy needs an explicit product
  requirement and tests for the new cost and duplicate-delivery behavior.

### Production scheduling

- Preserve the current UTC+8 schedule (08:00, 13:00, 20:00), login requirement and
  skipped missed slots unless a requested scheduling change explicitly replaces
  that policy. Flag a task action that points at a different pipeline from the one
  used in its acceptance evidence; a manual success alone does not validate the
  installed daemon, deduplication or automatic notification policy.

### Notification outcomes

- The normal background and desktop manual paths may classify a validated full
  primary tweet when its context supplement is unavailable. Pass an explicit
  context_unavailable marker to the model, label primary-only analysis in mail,
  and retain successful context on other tweets. Keep each context GET single
  attempt and the batch at one model request/one email. Never use this fallback
  for contradictory primary identity, timestamp or text, or reflected secrets.
- The installed background sends one summary for every successfully completed
  slot: include all new classifications, including IRRELEVANT, and send a status
  summary when there are no new tweets. Deduplication still skips repeated model
  calls. Manual testing sends a test summary; undeployed candidates retain their
  separate policies. Never report SMTP
  acceptance as inbox delivery, erase failed or uncertain receipts to force a retry,
  or claim failure-recovery behavior from an undeployed candidate.
