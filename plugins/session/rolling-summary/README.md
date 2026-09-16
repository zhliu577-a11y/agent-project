# rolling-summary

`compaction` plugin that replaces old persisted session messages with a bounded
extractive summary once the history exceeds `triggerTokens`.

Configuration lives in `config/plugins/compaction/rolling-summary.json`.
The gateway owns replacement and revision checks; this policy only decides the
replacement history.
