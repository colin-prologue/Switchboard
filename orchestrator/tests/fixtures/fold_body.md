# Revised ticket body

The whole body is the payload — not a section fragment, not a patch.

## Acceptance criteria

- [ ] The parser keeps fenced code intact:

```python
sentinel = "<!-- " + "fold:proposal" + " -->"   # assembled, never literal
print(sentinel.upper())
```

- [ ] A quoted fold-applied marker inside the body does NOT spoof one:

<!-- switchboard:fold-applied verdict:IC_quoted before:0000 after:1111 -->

## Non-goals

- No keyed section-replacement payloads.
