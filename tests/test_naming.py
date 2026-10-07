from ttl2fabric.naming import (
    acronyms_from_labels,
    humanize,
    lineage_tag,
    normalize,
    relationship_name,
    sanitize_identifier,
    tmdl_name,
    tmdl_ref,
    unique_name,
)


def test_humanize_with_acronyms():
    acr = acronyms_from_labels(["MDU Owner Flag", "CRM Customer Source Id", "MVNO Flag"])
    assert humanize("mduOwnerCustomerKey", acr) == "MDU Owner Customer Key"
    assert humanize("cRMCustomerSourceId", acr) == "CRM Customer Source Id"
    assert humanize("mVNOFlag", acr) == "MVNO Flag"
    assert humanize("mainAddressKey") == "Main Address Key"
    assert humanize("lastServiceRequestTypeLevel2Key") == "Last Service Request Type Level 2 Key"


def test_relationship_name_matches_fabric_convention():
    assert relationship_name("Customer_HasMainAddress_Address", "Address") == "CustomerHasMainAddress"
    assert relationship_name("Customer_HasParent_Customer", "Customer") == "CustomerHasParent"
    assert relationship_name("Weird", None) == "Weird"


def test_tmdl_quoting():
    assert tmdl_name("Customer") == "Customer"
    assert tmdl_name("Main Address Key") == "'Main Address Key'"
    assert tmdl_name("O'Brien") == "'O''Brien'"
    assert tmdl_ref("Customer", "Main Address Key") == "Customer.'Main Address Key'"


def test_misc_helpers():
    assert normalize("Main Address-Key") == "mainaddresskey"
    assert sanitize_identifier("1 bad name!") == "X_1_bad_name"
    assert unique_name("City Key", {"city key"}, " ") == "City Key 2"
    assert lineage_tag("a", "b") == lineage_tag("a", "b") != lineage_tag("a", "c")
