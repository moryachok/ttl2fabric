from ttl2fabric.catalog import Column
from ttl2fabric.model import Kind, Reason, PropertyOrigin
from ttl2fabric.resolver import match_column

COLS = [Column("Open Date", "timestamp"), Column("case prop", "string"), Column("Main_Address_Key", "string")]


def skips(res, kind=None, reason=None):
    return [s for s in res.skips if (kind is None or s.kind == kind) and (reason is None or s.reason == reason)]


def test_match_column_tiers():
    assert match_column(["Open Date", "openDate"], COLS, False, False).status == "exact"
    m = match_column(["Case Prop"], COLS, False, False)
    assert m.status == "case" and m.column.name == "case prop"
    assert match_column(["Case Prop"], COLS, True, False).status == "case_mismatch"
    m = match_column(["Main Address Key"], COLS, False, False)
    assert m.status == "not_found" and "fuzzy" in m.detail
    assert match_column(["Main Address Key"], COLS, False, True).status == "fuzzy"
    assert match_column(["Nope"], COLS, False, True).status == "not_found"
    dup = [Column("A", "string"), Column("a", "string")]
    assert match_column(["A "], dup, False, True).status == "ambiguous"


def test_unverified_conversion_binds_from_ttl(run, null_catalog):
    res = run(catalog=null_catalog, schemas=[])
    assert not res.verified
    names = {e.name for e in res.ontology.entities}
    assert names == {"Customer", "Address", "FinancialAccount"}
    assert {s.reason for s in skips(res, Kind.ENTITY)} == {Reason.NO_PHYSICAL_TABLE, Reason.COMPOSITE_KEY_UNSUPPORTED}
    customer = res.ontology.entity("Customer")
    assert customer.schema == "dbo" and customer.table_name == "customer__t"
    assert customer.property_by_name("Open Date").column == "Open Date"
    assert customer.property_by_name("Open Date").data_type == "dateTime"
    assert not res.unresolved


def test_verified_without_skip_flags_reports_unresolved(run):
    res = run()
    codes = {f.code for f in res.unresolved}
    assert "UNRESOLVED_TABLE" in codes and "UNRESOLVED_COLUMN" in codes
    assert res.ontology.entity("FinancialAccount") is not None  # emitted, but flagged


def test_skip_flags_cascade_and_reasons(run):
    res = run(skip_missing_tables=True, skip_missing_columns=True)
    assert {e.name for e in res.ontology.entities} == {"Customer", "Address"}
    assert not res.unresolved
    fa = [s for s in skips(res, Kind.ENTITY) if s.name == "FinancialAccount"][0]
    assert fa.reason == Reason.TABLE_NOT_FOUND
    cascaded = {s.name for s in skips(res, Kind.PROPERTY, Reason.ENTITY_SKIPPED) if s.entity == "FinancialAccount"}
    assert cascaded == {"Financial Account Key", "Balance"}
    missing = skips(res, Kind.PROPERTY, Reason.COLUMN_NOT_FOUND)
    assert [(s.entity, s.name) for s in missing] == [("Customer", "Missing Prop")]

    customer = res.ontology.entity("Customer")
    assert customer.property_by_name("Case Prop").column == "case prop"
    assert customer.property_by_name("MVNO Flag").data_type == "int64"  # type comes from the table
    assert any(f.code == "COLUMN_CASE_RESOLVED" for f in res.findings)

    rels = {r.name: r for r in res.ontology.relationships}
    assert set(rels) == {"CustomerHasMainAddress", "CustomerHasBillingAddress"}
    assert rels["CustomerHasBillingAddress"].from_column == "Billing Address Key"  # reversed join handled
    assert rels["CustomerHasMainAddress"].to_column == "Address Key"
    reasons = {s.name: s.reason for s in skips(res, Kind.RELATIONSHIP)}
    assert reasons["CustomerHasLegacyAddress"] == Reason.FK_COLUMN_UNRESOLVED
    assert reasons["CustomerHasCreditClass"] == Reason.TARGET_ENTITY_SKIPPED
    assert reasons["CustomerHasAccount"] == Reason.TARGET_ENTITY_SKIPPED
    assert reasons["CustomerHasParent"] == Reason.SELF_RELATIONSHIP
    assert reasons["AddressHasGarbage"] == Reason.JOIN_CONDITION_UNPARSEABLE
    assert reasons["AddressHasWrongTables"] == Reason.JOIN_TABLE_MISMATCH

    fk = {p.name for p in customer.properties if p.origin == PropertyOrigin.FOREIGN_KEY}
    # FKs of kept relationships + physically present FKs of skipped ones
    assert fk == {
        "Main Address Key",
        "Billing Address Key",
        "Credit Class Key",
        "Parent Customer Key",
        "Financial Account Key",
    }
    assert customer.property_by_name("Fax Number") is None


def test_case_sensitive_skips_case_mismatch(run):
    res = run(skip_missing_tables=True, skip_missing_columns=True, case_sensitive=True)
    s = [x for x in skips(res, Kind.PROPERTY) if x.name == "Case Prop"][0]
    assert s.reason == Reason.COLUMN_CASE_MISMATCH


def test_key_column_missing_skips_entity(source, catalog, run):
    catalog._columns[("bronze", "address__t")] = [catalog._columns[("bronze", "address__t")][1]]
    res = run(skip_missing_tables=True, skip_missing_columns=True)
    addr = [s for s in skips(res, Kind.ENTITY) if s.name == "Address"][0]
    assert addr.reason == Reason.KEY_COLUMN_UNRESOLVED
    assert all(r.to_entity != "Address" for r in res.ontology.relationships)
    assert any(s.entity == "Address" and s.reason == Reason.ENTITY_SKIPPED for s in res.skips)


def test_table_ambiguous_without_schema(run):
    res = run(schemas=[], skip_missing_tables=True, skip_missing_columns=True)
    cust = [s for s in skips(res, Kind.ENTITY) if s.name == "Customer"][0]
    assert cust.reason == Reason.TABLE_AMBIGUOUS


def test_filters_and_options(run):
    res = run(include_entities=["customer", "address__t"], skip_missing_tables=True, skip_missing_columns=True)
    assert {e.name for e in res.ontology.entities} == {"Customer", "Address"}
    assert any(s.reason == Reason.FILTERED_OUT for s in res.skips)

    res = run(subject_areas=["Finance"])
    assert {e.name for e in res.ontology.entities} == {"FinancialAccount"}

    res = run(skip_missing_tables=True, skip_missing_columns=True, one_relationship_per_pair=True)
    assert len(res.ontology.relationships) == 1
    assert any(s.reason == Reason.DUPLICATE_RELATIONSHIP_PAIR for s in res.skips)

    res = run(skip_missing_tables=True, skip_missing_columns=True, allow_self_relationships=True)
    parent = [r for r in res.ontology.relationships if r.name == "CustomerHasParent"][0]
    assert (parent.from_column, parent.to_column) == ("Parent Customer Key", "Customer Key")

    res = run(skip_missing_tables=True, skip_missing_columns=True, fk_properties=False)
    customer = res.ontology.entity("Customer")
    assert all(p.origin != PropertyOrigin.FOREIGN_KEY for p in customer.properties)
    assert ("Main Address Key", "string") in customer.hidden_columns

    res = run(skip_missing_tables=True, skip_missing_columns=True, include_unmapped_columns=True)
    customer = res.ontology.entity("Customer")
    assert customer.property_by_name("Fax Number").origin == PropertyOrigin.UNMAPPED
    assert customer.property_by_name("Blob") is None  # binary is unsupported

    res = run(skip_missing_tables=True, skip_missing_columns=True, entity_naming="label", subject_areas=["Customer"])
    assert {e.name for e in res.ontology.entities} == {"Customer", "Address"}


def test_entity_label_naming(source, null_catalog):
    from ttl2fabric.resolver import Options, resolve

    res = resolve(source, null_catalog, Options(entity_naming="label"), "T")
    assert res.ontology.entity("Financial Account") is not None


def test_key_type_rule_and_override(catalog, run):
    from ttl2fabric.catalog import Column

    cols = catalog._columns[("bronze", "address__t")]
    catalog._columns[("bronze", "address__t")] = [Column("Address Key", "timestamp")] + cols[1:]
    res = run(skip_missing_tables=True, skip_missing_columns=True)
    assert [s.reason for s in skips(res, Kind.ENTITY) if s.name == "Address"] == [Reason.KEY_TYPE_UNSUPPORTED]
    res = run(skip_missing_tables=True, skip_missing_columns=True, allow_any_key_type=True)
    assert res.ontology.entity("Address") is not None
    # FK (string) no longer matches the dateTime key -> relationship skipped, not broken
    assert any(s.reason == Reason.FK_TYPE_MISMATCH for s in res.skips)


def test_catalog_without_columns_is_unreadable_not_a_crash(catalog, run):
    del catalog._columns[("bronze", "address__t")]
    res = run(skip_missing_tables=True, skip_missing_columns=True)
    assert [s.reason for s in skips(res, Kind.ENTITY) if s.name == "Address"] == [Reason.TABLE_SCHEMA_UNREADABLE]
    res = run()
    assert any(f.code == "UNRESOLVED_TABLE_SCHEMA" for f in res.findings)


def test_unresolved_findings_dropped_with_skipped_items(catalog, run):
    from ttl2fabric.catalog import Column

    # entity skipped late (key type) -> its UNRESOLVED_COLUMN must not survive
    cols = catalog._columns[("bronze", "customer__t")]
    catalog._columns[("bronze", "customer__t")] = [Column("Customer Key", "timestamp")] + cols[1:]
    res = run(skip_missing_tables=True)
    assert res.ontology.entity("Customer") is None
    assert not [f for f in res.unresolved if f.entity == "Customer"]
    catalog._columns[("bronze", "customer__t")] = cols

    # relationship skipped after its FK was flagged unresolved -> finding dropped
    res = run(skip_missing_tables=True, one_relationship_per_pair=True)
    assert not [f for f in res.unresolved if f.name == "CustomerHasLegacyAddress"]


def test_self_relationship_orientation_uses_physical_key_name(source, catalog):
    import copy

    from ttl2fabric.resolver import Options, resolve

    src = copy.deepcopy(source)
    customer = next(c for c in src.classes.values() if c.local_name == "Customer")
    next(dp for dp in customer.data_properties if dp.label == "Customer Key").physical_name = "custId"
    rel = next(op for op in src.object_properties if op.local_name == "Customer_HasParent_Customer")
    rel.join_condition = "customer__t.custId = customer__t.parentCustomerKey"
    opts = Options(schemas=["bronze"], skip_missing_tables=True, skip_missing_columns=True, allow_self_relationships=True)
    res = resolve(src, catalog, opts, "T")
    parent = [r for r in res.ontology.relationships if r.name == "CustomerHasParent"][0]
    assert (parent.from_column, parent.to_column) == ("Parent Customer Key", "Customer Key")


def test_property_metadata_defaults(run):
    res = run(skip_missing_tables=True, skip_missing_columns=True)
    customer = res.ontology.entity("Customer")
    key = customer.property_by_name("Customer Key")
    assert key.description == "Unique id of the customer."
    assert key.annotations == {"physicalDataPropertyName": "customerKey", "sqlDataType": "VARCHAR(256)"}
    fk = customer.property_by_name("Main Address Key")
    assert fk.description.startswith("Foreign key to Address (Address Key). Identifies the main address")
    assert fk.annotations["references"] == "Address.Address Key"
    assert fk.annotations["joinCondition"] == "customer__t.mainAddressKey = address__t.addressKey"
    assert fk.annotations["classification"] == "Foreign Key"


def test_property_metadata_opt_out_and_exclude(run):
    res = run(skip_missing_tables=True, skip_missing_columns=True, property_descriptions=False, property_annotations=False)
    customer = res.ontology.entity("Customer")
    assert all(p.description is None and not p.annotations for p in customer.properties)
    assert customer.annotations  # entity metadata is unaffected

    res = run(skip_missing_tables=True, skip_missing_columns=True, annotation_exclude=["sqlDataType", "CLASSID"])
    customer = res.ontology.entity("Customer")
    assert "sqlDataType" not in customer.property_by_name("Customer Key").annotations
    assert "classId" not in customer.annotations and "subjectArea" in customer.annotations
