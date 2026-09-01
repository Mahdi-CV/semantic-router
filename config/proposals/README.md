# Configuration proposal intents

Proposal intents are bounded, reviewable requests for `vllm-sr config propose`.
They contain a human-readable goal and select maintained fragments by ID. The
installed vLLM-SR CLI defines this small input contract; users do not need to
choose a separate intent schema version. Intent files cannot contain arbitrary
Router fields, credentials, deployment endpoints, or instructions to activate
a configuration.

Generate the maintained keyword-signal example from the repository root:

```bash
vllm-sr config propose \
  --config config/recipes/knowledge/config.yaml \
  --intent config/proposals/keyword-signals.yaml \
  --endpoint http://localhost:8080 \
  --output /tmp/keyword-signal-proposal
```

Run the example from the repository root. `--fragment-root` defaults to the
maintained `config/fragments` tree. `--output` is required so the command never
creates review artifacts in an implicit working-tree location.

The Router endpoint must expose the `v1` structured validation and diff contract
from #3477. The command sends the candidate to
`POST /api/v1/config/validate` with `compare_to_active: true` through the shared
authenticated management client; it never calls an update, apply, deploy, or
activation endpoint. Use a base configuration that represents the Router's
active snapshot so the returned diff has the intended comparison identity.

:::warning
Current `main` does not yet implement `compare_to_active` or return validation
contract `v1`. Until #3477 lands, the command stops before writing artifacts.
The successful artifact path is contract-tested with a stubbed v1 validation
response; it has not been exercised end to end against a current-main Router.
:::

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

The generated provenance records the vLLM-SR generator version, the base
configuration's canonical version, intent and goal digests, maintained fragment
revisions, proposal digest, and Router validation contract. Those versions are
tool-owned metadata; they are not fields the intent author must supply.
