# ttl2fabric

Convert an OWL/Turtle ontology (e.g. the Telco ontology) into a **Fabric IQ Ontology (v2)**
item definition, **bound to physical lakehouse tables**, so you can create a fully bound ontology item
at scale. Optionally it validates every entity, property, and relationship against the real lakehouse via
the Fabric / OneLake APIs, skips what doesn't physically exist, and logs every skip with its reason.

Outputs of one `convert` run:

| File | What it is |
|---|---|
| `definition/` | Fabric Ontology v2 item definition (TMDL parts, same layout as `getDefinition` of a v2 item). Deployable with `ttl2fabric deploy`. |
| `<name>.ttl` | The same model in the TTL vocabulary that Fabric v2 exports (`fabric:` IRIs, `urn:custom:*`, `Entity__Label` qualified property IRIs). Schema only — TTL carries no bindings. |
| `skipped.csv` / `skipped.jsonl` | Every skipped entity / property / relationship with a reason code and detail. |
| `findings.csv` | Non-fatal notes (FK property added, type taken from table, case resolved, **UNRESOLVED** bindings …). |
| `report.json` | Counts, options, lakehouse, per-entity summary. `deploy` reads it. |
| `catalog.json` | Snapshot of the physical catalog that was used (when validating). |
| `ttl2fabric.log` | Full debug log, including one `SKIP` line per skipped item. |

> Why TMDL and not just TTL? A Fabric v2 TTL export contains only the schema (classes, properties,
> relationships). Table bindings live in the item definition: `tables/*.tmdl` (DirectLake partition →
> `schema.table` + columns), `entities/*.tmdl` (key + `property → valueColumn`), `relationships.tmdl`
> (FK column → key column) and `entityRelationships.tmdl`. `ttl2fabric` emits both formats from the same resolved model.

---

## 1. Setup

```bash
cd ttl2fabric
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt      # runtime
pip install -r requirements-dev.txt  # + pytest (optional)

az login                             # only needed for --workspace/--lakehouse, catalog, deploy
```

Python 3.10+ is required. Authentication uses `azure-identity`:

- `--auth cli` (default): uses your `az login` session.
- `--auth default`: `DefaultAzureCredential` (service principal environment variables, managed identity, …).
- `--auth interactive`: opens a browser.

The identity needs read access to the lakehouse (validation) and at least *Contributor* on the target workspace (deploy).

## 2. How the conversion and binding work

| Input TTL (annotation vocabulary is auto-detected) | Fabric Ontology v2 |
|---|---|
| `owl:Class` + `:physicalClassName "customer__t"` | entity type `Customer` backed by table `<schema>.customer__t` (DirectLake partition) |
| `:primaryKey "Customer Key"` | `keyProperty: 'Customer Key'` (single key, must be string/int64) |
| `rdfs:comment`, `:synonyms "A; B"`, other class annotations | entity description, `synonym` lines, `annotation k = v` (+ `urn:custom:k` in TTL) |
| `owl:DatatypeProperty` (`rdfs:label`, `:physicalDataPropertyName`, `rdfs:range`) | property named after the **label**, bound to the matching physical column; data type comes from the **table** (falls back to the xsd range) |
| property `rdfs:comment` and metadata (`dataPropertyId`, `physicalDataPropertyName`, `sqlDataType`, `classification`, `synonyms`, `businessRule`, …) | property **Description** (`///` line) and **Additional metadata** (`annotation k = v` inside the property; `urn:custom:k` in TTL), as shown in Fabric's *Edit metadata* dialog |
| `:hasEnumerationValue [ :enumerationValue "x" ]` | `enumerationValues = a; b; c` property annotation (value definitions/synonyms are not carried over) |
| `owl:ObjectProperty` + `:joinCondition "customer__t.mainAddressKey = address__t.addressKey"` | table relationship `Customer.'Main Address Key' → Address.'Address Key'` + entity relationship `CustomerHasMainAddress` |
| FK columns used by joinConditions | exposed as entity properties (as Fabric does), e.g. `Main Address Key`, with a description ("Foreign key to Address (Address Key). …") and metadata (`references`, `joinCondition`, `cardinality`, `objectPropertyId`, …) |
| Business terms (`owl:NamedIndividual`) | not converted (reported in `report.json → source.notConverted`) |

**Column matching.** For each property the candidates are the `rdfs:label` (first) and the
`physicalDataPropertyName` (use `--column-naming physical` to flip the priority). FK columns from
joinConditions are tried as the known label for that physical name, then a humanized form (`mduOwnerCustomerKey` →
`MDU Owner Customer Key`, using acronyms found in labels), then the raw name. Matching tiers:

1. exact name;
2. same name ignoring letter case — accepted and logged as `COLUMN_CASE_RESOLVED`, or with `--case-sensitive` treated as a mismatch (`COLUMN_CASE_MISMATCH`);
3. only with `--fuzzy-columns`: same name ignoring spaces/underscores/punctuation.

**Tables** are searched in the `--schema` list (in order) or in all schemas. A table found in several schemas
without `--schema` is skipped as `TABLE_AMBIGUOUS`.

**Fail-safe by default.** Without skip flags, anything missing is still emitted but recorded as an
`UNRESOLVED_*` finding, `convert` exits with code **2**, and `deploy` refuses the output (unless `--force`).
Use the skip flags to drop what doesn't exist instead.

## 3. Examples

All examples assume the venv is active and you are in the `ttl2fabric/` folder.

### 3.1 Simplest run — TTL only, no Fabric access

```bash
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoMain --format ttl
```

Writes `build/TelcoMain/TelcoMain.ttl` plus reports. Entities without a table/key are still skipped (with reasons).

### 3.2 Offline TMDL with known lakehouse IDs (bindings NOT verified)

```bash
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoMain \
  --workspace-id <workspace-guid> \
  --lakehouse-id <lakehouse-guid> \
  --lakehouse-name ontology_lakehouse --schema bronze
```

Columns are bound by label (`--column-naming label`) without checking that they exist. `report.json` marks the run as `verified: false`.

### 3.3 Validate against the lakehouse and skip anything missing (recommended)

```bash
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoMain \
  --workspace customers --lakehouse ontology_lakehouse --schema bronze \
  --skip-missing-tables --skip-missing-columns
```

- Entities whose table doesn't exist → skipped (`TABLE_NOT_FOUND`), together with all their properties (`ENTITY_SKIPPED`) and relationships (`SOURCE/TARGET_ENTITY_SKIPPED`).
- Properties whose column doesn't exist → skipped (`COLUMN_NOT_FOUND`), e.g. `Customer.No Binded Test`.
- Relationships whose FK column doesn't exist → skipped (`FK_COLUMN_UNRESOLVED`).

`--workspace`/`--lakehouse` accept names or GUIDs.

### 3.4 Strict mode — exact, case-sensitive names only

```bash
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoMain \
  --workspace customers --lakehouse ontology_lakehouse --schema bronze --strict
# --strict == --skip-missing-tables --skip-missing-columns --case-sensitive
```

A property mapped to `Open date` when the column is `Open Date` is skipped as `COLUMN_CASE_MISMATCH`.

### 3.5 Tolerant matching (spaces / underscores / punctuation)

```bash
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoMain \
  --workspace customers --lakehouse ontology_lakehouse --schema bronze \
  --skip-missing-tables --skip-missing-columns --fuzzy-columns
```

`customerKey` can now bind to `Customer_Key` (logged as `COLUMN_FUZZY_RESOLVED`). Without the flag, the skip detail suggests the closest column.

### 3.6 Convert a subset

```bash
# only two entities (local name, label or physical table name; repeat or comma-separate)
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name CustomerAddress \
  --entities Customer,Address --workspace customers --lakehouse ontology_lakehouse --schema bronze --strict

# a whole subject area, minus one class
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoCustomer \
  --subject-areas Customer --exclude-entities CustomerDailyProfile \
  --workspace customers --lakehouse ontology_lakehouse --schema bronze --skip-missing-tables --skip-missing-columns
```

### 3.7 Match what Fabric creates in the UI (add all physical columns)

```bash
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoMain \
  --workspace customers --lakehouse ontology_lakehouse --schema bronze --strict --include-unmapped-columns
```

Columns that the TTL doesn't describe (e.g. `Fax Number`, `Latitude`) become properties too. With this flag
the generated `tables/*.tmdl` for Customer/Address are identical to those of the hand-built `TelcoMainV2` item.

### 3.8 Offline validation with a catalog snapshot

```bash
# once, with Fabric access
python -m ttl2fabric catalog --workspace customers --lakehouse ontology_lakehouse --schema bronze -o catalog.json

# later / elsewhere, no Fabric access needed
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoMain \
  --catalog-file catalog.json --schema bronze --strict
```

`--catalog-file` also accepts a CSV export of the SQL endpoint's `INFORMATION_SCHEMA.COLUMNS`
(`TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE`). In that case pass the lakehouse identity with
`--workspace-id --lakehouse-id --lakehouse-name`, because the CSV doesn't contain it.

### 3.9 Relationship and modelling options

```bash
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoMain \
  --workspace customers --lakehouse ontology_lakehouse --schema bronze --strict \
  --allow-self-relationships --one-relationship-per-pair --no-fk-properties \
  --entity-naming label --annotation-exclude dataPropertyId,classId
```

- `--allow-self-relationships`: keep relationships such as Customer → Customer (parent customer).
- `--one-relationship-per-pair`: keep only the first relationship between the same two entities.
- `--no-fk-properties`: keep FK columns on the table but don't expose them as entity properties.
- `--entity-naming label`: `Financial Account` instead of `FinancialAccount`.
- `--annotation-exclude`: leave the listed metadata keys out of entities and properties.

### 3.10 Property metadata (description + additional metadata)

Property descriptions and metadata are emitted by default. In the generated `entities/<Entity>.tmdl` each property looks like this, and Fabric shows it in the property's **Edit metadata** dialog:

```
	/// Specifies the date on which the customer has been acquired, as per the CRM system.
	property 'Acquisition Date'
		dataType: dateTime
		lineageTag: ...

		backingConfiguration
			valueColumn: Customer.'Acquisition Date'

		annotation dataPropertyId = 27432

		annotation sqlDataType = DATE

		annotation synonyms = Customer Acquisition Date; Customer Joined Date; ...
```

```bash
# schema + bindings only, no property metadata
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoLean \
  --workspace customers --lakehouse ontology_lakehouse --schema bronze --strict \
  --no-property-descriptions --no-property-annotations

# keep the metadata but drop internal ids
python -m ttl2fabric convert inputs/Telco_Ontology_v15.ttl --name TelcoMain \
  --workspace customers --lakehouse ontology_lakehouse --schema bronze --strict \
  --annotation-exclude dataPropertyId,objectPropertyId,classId
```

### 3.11 Create or update the ontology item in Fabric

```bash
# inspect the exact request first (writes build/TelcoMain/envelope.json, sends nothing)
python -m ttl2fabric deploy build/TelcoMain --workspace customers --dry-run

# create it (shows a preview and asks you to type 'yes')
python -m ttl2fabric deploy build/TelcoMain --workspace customers

# non-interactive (CI), custom display name
python -m ttl2fabric deploy build/TelcoMain --workspace customers --name TelcoMain_Prod --yes

# re-provision an item that already exists (same name) by replacing its definition
python -m ttl2fabric deploy build/TelcoMain --workspace customers --name TelcoMainV2FromCode --update-existing

# put the item in a workspace folder (path or folder id); --create-folder creates missing levels
python -m ttl2fabric deploy build/TelcoMain --workspace customers --name TelcoMain \
  --folder data-services/ontology
python -m ttl2fabric deploy build/TelcoMain --workspace customers --name TelcoMain \
  --folder data-services/ontology/telco --create-folder

# update an existing item and move it to another folder ('/' = workspace root)
python -m ttl2fabric deploy build/TelcoMain --workspace customers --name TelcoMainV2FromCode \
  --update-existing --folder data-services/ontology
```

**Folders.** `--folder` accepts a slash-separated path (matched exactly, then ignoring case), a folder GUID, or `/` for
the workspace root. On create the item is placed there directly. Fabric puts the companion items it creates
(graph model, eventhouse, KQL database) in the same folder. With `--update-existing`, an item in another folder is
moved there (its companions move with it). Without `--folder`, an updated item stays where it is. A missing folder
is an error that lists the subfolders that do exist, unless you pass `--create-folder`.

By default `deploy` creates a **new** item and stops if an Ontology with that name already exists. With
`--update-existing` it replaces the existing item's whole definition instead. It:

1. downloads the current definition to `build/<name>/backups/<item>_<id>_<timestamp>/`, so you can restore it;
2. reuses the existing lineageTags of matching tables, columns, entities, properties and relationships, so entity
   and property IDs (the ones in Fabric URLs) stay the same;
3. shows a change preview (entities/properties/relationships before → after, removed entities) and asks you to
   type `yes` (or pass `--yes`).

The item ID is kept. Entities that aren't in the new definition are removed. `deploy` refuses outputs with unresolved
bindings unless you pass `--force`. It waits for the long-running operation. On success it prints the ontology item
URL and its MCP server endpoint:

```
Created Ontology 'TelcoMain' (<item-id>) in folder data-services/ontology
  Ontology item : https://app.fabric.microsoft.com/groups/<workspace-id>/ontologies/<item-id>
  MCP endpoint  : https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/<workspace-id>/items/<item-id>/ontologyEndpoint
```

To use the MCP endpoint (for example from VS Code agent mode), add it as an HTTP MCP server. See
[Consume ontology as an MCP server](https://learn.microsoft.com/fabric/iq/ontology/how-to-use-ontology-mcp-server).
The server exposes `list_ontology_entities` (entities, properties, keys, descriptions and metadata),
`list_ontology_rules` and `ask_ontology` (natural-language questions over the bound data).

### 3.12 Troubleshooting runs

```bash
# every SKIP/NOTE line on the console (they are always in ttl2fabric.log)
python -m ttl2fabric convert ... --log-level DEBUG

# what was skipped and why
column -s, -t < build/TelcoMain/skipped.csv | less -S
grep ',COLUMN_' build/TelcoMain/skipped.csv
python -c "import json;print(json.dumps(json.load(open('build/TelcoMain/report.json'))['skipped'],indent=2))"
```

## 4. Command reference

### `convert`

| Flag | Meaning |
|---|---|
| `input` | Ontology file (`.ttl`; `.rdf/.owl/.nt/.jsonld` also parse) |
| `-o, --output` | Output folder (default `build/<name>`). Each run regenerates `definition/` and removes it when no TMDL is written, e.g. with `--format ttl`. `deploy` also checks a fingerprint stored in `report.json`. |
| `--name` | Ontology display name (default: sanitized ontology label) |
| `--format both\|tmdl\|ttl` | What to write (default `both`) |
| `--ontology-id` | GUID used in the TTL base IRI `https://fabric.microsoft.com/ontology/<id>` (default: deterministic from `--name`) |
| `--vocab-ns` | Annotation namespace (default: the file's `:` prefix) |
| `--workspace`, `--lakehouse` | Live validation via Fabric REST + OneLake (names or GUIDs) |
| `--catalog-file` | Offline validation from `catalog.json` or an INFORMATION_SCHEMA CSV |
| `--schema` | Schema(s) to search, in priority order (repeatable / comma list) |
| `--workspace-id/-name`, `--lakehouse-id/-name`, `--sql-endpoint` | Lakehouse identity overrides (needed for TMDL without live access) |
| `--skip-missing-tables` | Skip entities whose table is missing (cascades to properties and relationships) |
| `--skip-missing-columns` | Skip properties and relationships whose column is missing or doesn't match |
| `--case-sensitive` | Names that differ only by letter case don't match |
| `--fuzzy-columns` | Also match ignoring spaces / underscores / punctuation |
| `--strict` | `--skip-missing-tables --skip-missing-columns --case-sensitive` |
| `--entities`, `--exclude-entities`, `--subject-areas` | Select classes |
| `--entity-naming local\|label` | Entity names from the class local name (default) or the label |
| `--column-naming label\|physical` | Which TTL name is tried first as the column name |
| `--no-fk-properties` | Don't expose FK columns as properties |
| `--include-unmapped-columns` | Add physical columns not described by the TTL |
| `--allow-self-relationships` | Keep relationships from an entity to itself (skipped by default) |
| `--one-relationship-per-pair` | Keep only the first relationship per (source, target) |
| `--allow-any-key-type` | Allow keys that are not string/int64 (e.g. the date key of `Calendar`) |
| `--no-property-descriptions`, `--no-property-annotations` | Leave out property descriptions / additional metadata (both emitted by default) |
| `--annotation-exclude` | Metadata keys to leave out of entities and properties (comma list, case-insensitive) |
| `--log-level`, `--log-file`, `--auth` | Logging / authentication |

### `catalog`

`--workspace`, `--lakehouse` (required), `--schema`, `--tables`, `-o catalog.json`. Lists tables through
OneLake and reads each table's columns (logical names and types) from its Delta transaction log, including
checkpoints (via `pyarrow`).

### `deploy`

`path` (convert output folder), `--workspace` (required), `--name`, `--description`, `--dry-run`, `--yes`, `--force`,
`--update-existing` (replace the definition of an existing item with the same name; backup + preview + confirmation),
`--folder` (path, folder id or `/`), `--create-folder`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error (authentication, API, invalid input, …) |
| 2 | `convert` finished but some bindings are **UNRESOLVED** (see `findings.csv`) |

## 5. Skip reason codes (`skipped.csv → reason`)

| Kind | Reason | When |
|---|---|---|
| entity | `FILTERED_OUT` | Excluded by `--entities/--exclude-entities/--subject-areas` |
| entity | `NO_PHYSICAL_TABLE` | Class has no `physicalClassName` (reference-only class) |
| entity | `KEY_NOT_DEFINED` / `COMPOSITE_KEY_UNSUPPORTED` | No `primaryKey`, or a multi-column key |
| entity | `TABLE_NOT_FOUND` / `TABLE_CASE_MISMATCH` | Table missing (with `--skip-missing-tables`) / differs only by case (with `--case-sensitive`) |
| entity | `TABLE_AMBIGUOUS` | Same table name in several schemas; pass `--schema` |
| entity | `TABLE_SCHEMA_UNREADABLE` | Table found but its Delta log couldn't be read |
| entity | `KEY_PROPERTY_NOT_FOUND` / `KEY_COLUMN_UNRESOLVED` / `KEY_TYPE_UNSUPPORTED` | Key isn't a data property / its column is missing / its type isn't string or int64 |
| entity | `DUPLICATE_ENTITY_NAME` | Two classes map to the same entity name |
| property | `ENTITY_SKIPPED` | Its entity was skipped (one row per property) |
| property | `COLUMN_NOT_FOUND` / `COLUMN_CASE_MISMATCH` / `COLUMN_AMBIGUOUS` | No matching column / differs only by case / matches several columns |
| property | `UNSUPPORTED_COLUMN_TYPE` | Binary / array / map / struct column |
| property | `DUPLICATE_PROPERTY_NAME` / `DUPLICATE_COLUMN` | Label repeated in a class / column already bound to another property |
| relationship | `SOURCE_ENTITY_SKIPPED` / `TARGET_ENTITY_SKIPPED` | An end of the relationship was not created |
| relationship | `SELF_RELATIONSHIP` | Source == target (use `--allow-self-relationships`) |
| relationship | `MISSING_DOMAIN_OR_RANGE` / `JOIN_CONDITION_MISSING` / `JOIN_CONDITION_UNPARSEABLE` | Incomplete object property; joinCondition must be `a.col = b.col` |
| relationship | `JOIN_TABLE_MISMATCH` | joinCondition doesn't reference the domain and range tables |
| relationship | `TARGET_COLUMN_NOT_KEY` | Join column on the target isn't the target's key |
| relationship | `FK_COLUMN_UNRESOLVED` | FK column missing / case mismatch / ambiguous on the source table |
| relationship | `FK_TYPE_MISMATCH` | FK column type differs from the target key type |
| relationship | `DUPLICATE_RELATIONSHIP_PAIR` | Dropped by `--one-relationship-per-pair` |

Findings (`findings.csv → code`): `UNRESOLVED_TABLE`, `UNRESOLVED_TABLE_SCHEMA`, `UNRESOLVED_COLUMN`,
`UNRESOLVED_FK_COLUMN` (these block deploy), `TABLE_CASE_RESOLVED`, `COLUMN_CASE_RESOLVED`, `COLUMN_FUZZY_RESOLVED`,
`TYPE_FROM_TABLE`, `FK_PROPERTY_ADDED`, `FK_PROPERTY_RENAMED`, `UNMAPPED_COLUMN_ADDED`, `RELATIONSHIP_RENAMED`.

## 6. Known limitations / notes

- Fabric IQ Ontology is in preview. The TMDL layout reproduces a live v2 `getDefinition` (CRLF, tabs, ordinal
  property order, property lineageTag = column lineageTag). LineageTags are deterministic, so re-runs are stable.
- Verified with a live `createItem`: entity and property descriptions and additional metadata (including values
  containing `=`, quotes and brackets) are stored unchanged and shown in Fabric's *Edit metadata* dialog.
- Not yet confirmed against a live `createItem` for large models: whether v2 accepts several relationships
  between the same two entities (`--one-relationship-per-pair` avoids it), self-relationships (skipped by default),
  entity names with spaces (`--entity-naming label`), and non-string/int64 keys (skipped by default).
- Composite keys aren't supported. Add a single surrogate key column to the table and set `primaryKey` to it.
- Physical columns must be in a managed Delta table in a lakehouse. Column mapping (names with spaces) is fine,
  and the tool always uses logical column names.
- Enumerations and business terms in the TTL are reported but not converted.
- The TTL output uses a pre-creation base IRI (`--ontology-id`); Fabric assigns the real item id on creation.

## 7. Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

```
ttl2fabric/
  cli.py           argparse entry point (convert | catalog | deploy)
  parser.py        rdflib -> source model (annotation vocabulary auto-detected)
  resolver.py      binding + validation + skip policies (the core)
  catalog.py       physical catalog: live OneLake/Delta log, JSON/CSV file, or none; type mapping
  fabric_client.py Fabric REST + OneLake DFS client, LRO polling, retries
  emit_tmdl.py     TMDL item definition writer
  emit_ttl.py      Fabric-style TTL writer
  deploy.py        create / update (backup, ID-preserving) with preview + confirm gate, folder placement
  folders.py       workspace folder path resolution / creation
  mcp_endpoint.py  ontology item URL + MCP endpoint URL
  report.py        skipped.csv/jsonl, findings.csv, report.json, console summary
tests/             pytest suite with a synthetic ontology + catalog (no customer data)
```

`inputs/`, `exports/` and `build/` are git-ignored. Customer files never get committed.
