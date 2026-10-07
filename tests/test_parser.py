def test_parse_classes_and_properties(source):
    names = {c.local_name for c in source.classes.values()}
    assert {"Customer", "Address", "FinancialAccount", "DailyAgg", "CreditClass"} <= names
    assert "EnumerationValue" not in names and "BusinessTerm" not in names
    customer = next(c for c in source.classes.values() if c.local_name == "Customer")
    assert customer.physical_table == "customer__t"
    assert customer.primary_key == ["Customer Key"]
    assert customer.synonyms == ["Customer", "Client", "Account Holder"]
    assert list(customer.annotations)[:2] == ["classId", "subjectArea"]
    assert "synonyms" not in customer.annotations
    labels = [dp.label for dp in customer.data_properties]
    assert labels == sorted(labels) and "Missing Prop" in labels
    daily = next(c for c in source.classes.values() if c.local_name == "DailyAgg")
    assert daily.primary_key == ["Aggregate Date", "Customer Key"]


def test_unconverted_constructs_are_reported(source):
    text = " ".join(source.unconverted)
    assert "enumeration" in text and "meta classes" in text


def test_property_metadata_is_parsed_and_ordered(source):
    customer = next(c for c in source.classes.values() if c.local_name == "Customer")
    key = next(dp for dp in customer.data_properties if dp.label == "Customer Key")
    assert key.comment == "Unique id of the customer."
    assert list(key.annotations) == ["physicalDataPropertyName", "sqlDataType"]
    ssid = next(dp for dp in customer.data_properties if dp.label == "Source System Id")
    assert ssid.annotations["enumerationValues"] == "CRM"
