# The skills

Four Claude Code skills covering the loop: open a Power BI file, prove the
extraction, design something better, then answer questions against it.

## Install

```bash
cp -r skills/pbi-* ~/.claude/skills/          # available in every project
# or
cp -r skills/pbi-* /path/to/project/.claude/skills/
```

Each is a folder with a `SKILL.md` carrying `name` and `description` frontmatter.
Claude Code reads the description to decide when a skill applies, so the
descriptions are written as *when to use this*, not *what this is*.

## The four

| skill | reach for it when |
|---|---|
| `pbi-extract` | someone has a `.pbix` or `.pbip` and wants what is inside it |
| `pbi-replicate` | the rebuild has to be shown to be faithful before anyone trusts it |
| `pbi-redesign` | the ask is "what does this actually say", not "rebuild this" |
| `pbi-chat` | any question about the numbers in the file |

They chain: extract, then replicate or redesign, with chat available throughout.

## Where the tools live

The skills call the scripts in `tools/` at this project's root. If you install
the skills somewhere else, either copy `tools/` alongside them or edit the paths
in each `SKILL.md`. They are plain `python3 tools/<name>.py` invocations with no
hidden state, so moving them is a matter of pointing at the right directory.

## Why the descriptions read the way they do

A skill is only useful if it fires at the right moment. `pbi-chat` says "answer
questions against an extracted Power BI model ... when someone asks anything
about the numbers in their Power BI file", because that is the sentence a user
actually types. A description that said "a DuckDB semantic layer CLI" would
never match.

The bodies are written the same way: the traps first, the commands second. Every
one of them is a mistake that produces a plausible wrong answer rather than an
error - a visual-level filter on one visual, a sort-by column reached through
another table, a placeholder member that absorbs a whole channel, a fiscal year
that stops mid-June. Those are the parts worth carrying between sessions.
