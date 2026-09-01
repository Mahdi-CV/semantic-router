# Configuration proposal intents

Proposal intents are bounded, reviewable requests for `vllm-sr config propose`.
They select maintained fragments by ID; they do not contain arbitrary Router
fields, credentials, deployment endpoints, or instructions to activate a
configuration.

Generate the maintained keyword-signal example from the repository root:

```bash
vllm-sr config propose \
  --config config/recipes/knowledge/config.yaml \
  --intent config/proposals/keyword-signals.yaml \
  --endpoint http://localhost:8080 \
  --output /tmp/keyword-signal-proposal
```

The Router endpoint must expose the `v1` structured validation and diff contract
from #3477. The command sends the candidate to
`POST /config/router/validate` with `compare_to_active: true`; it never calls an
update, apply, deploy, or activation endpoint. Use a base configuration that
represents the Router's active snapshot so the returned diff has the intended
comparison identity.

Review these artifacts before taking any separate apply or activation action:

- `proposed-config.yaml`: the complete canonical proposal;
- `proposal-diff.json`: the Router's bounded, structured, redacted diff;
- `provenance.json`: versioned base, intent, source, and proposal identities;
- `validation.json`: the Router's complete structured validation receipt.

Generation fails when a fragment conflicts with an existing value, references
an unsupported ID, produces invalid canonical configuration, or reaches a
Router without the required validation contract. The command never changes the
base configuration or active Router state.

The proposed YAML preserves the base configuration's semantic fields and list
ordering, but the Router emits it in normalized canonical formatting. Comments
and presentation-only formatting from the source file are not retained.
