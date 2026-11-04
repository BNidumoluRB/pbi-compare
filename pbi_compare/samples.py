"""Small but realistic report/model definitions used by the tests and the demo mode."""
import json

T = "\t"

TMDL_SALES = """table Sales
\tlineageTag: 1

\tmeasure 'Total Sales' = SUM(Sales[Amount])
\t\tformatString: #,0.00

\tmeasure 'Order Count' = DISTINCTCOUNT(Sales[OrderID])

\tmeasure 'Avg Order Value' = DIVIDE([Total Sales], [Order Count])

\tmeasure 'West Sales' =
\t\t\tCALCULATE(
\t\t\t    [Total Sales],
\t\t\t    Customer[Region] = "West"
\t\t\t)

\tmeasure 'Running Total' = ```
\t\t\tVAR d = MAX('Date'[Date])
\t\t\tRETURN CALCULATE([Total Sales], FILTER(ALL('Date'), 'Date'[Date] <= d))
\t\t\t```

\tcolumn OrderID
\t\tdataType: int64
\t\tsourceColumn: ORDER_ID

\tcolumn Amount
\t\tdataType: decimal
\t\tsourceColumn: AMOUNT

\tcolumn CustomerKey
\t\tdataType: int64
\t\tisHidden
\t\tsourceColumn: CUSTOMER_KEY

\tcolumn OrderDate
\t\tdataType: dateTime
\t\tsourceColumn: ORDER_DATE

\tcolumn 'Net Amount' = Sales[Amount] * 0.9
\t\tdataType: decimal

\tpartition Sales = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Snowflake.Databases("acme.snowflakecomputing.com", "ANALYTICS_WH"),
\t\t\t\t    DB = Source{[Name=DatabaseName,Kind="Database"]}[Data],
\t\t\t\t    SCH = DB{[Name="SALES",Kind="Schema"]}[Data],
\t\t\t\t    TBL = SCH{[Name="FACT_SALES",Kind="Table"]}[Data],
\t\t\t\t    Renamed = Table.RenameColumns(TBL, {{"AMT_USD", "AMOUNT"}})
\t\t\t\tin
\t\t\t\t    Renamed
"""

TMDL_CUSTOMER = """table Customer

\tcolumn CustomerKey
\t\tdataType: int64
\t\tsourceColumn: CUSTOMER_KEY

\tcolumn Region
\t\tdataType: string
\t\tsourceColumn: REGION

\tcolumn 'Customer Name'
\t\tdataType: string
\t\tsourceColumn: CUSTOMER_NAME

\tpartition Customer = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Snowflake.Databases("acme.snowflakecomputing.com", "ANALYTICS_WH"),
\t\t\t\t    DB = Source{[Name=DatabaseName,Kind="Database"]}[Data],
\t\t\t\t    SCH = DB{[Name="SALES",Kind="Schema"]}[Data],
\t\t\t\t    V = SCH{[Name="DIM_CUSTOMER",Kind="View"]}[Data]
\t\t\t\tin
\t\t\t\t    V
"""

TMDL_DATE = """table Date

\tcolumn Date
\t\tdataType: dateTime
\t\tsourceColumn: DATE_KEY

\tcolumn Year
\t\tdataType: int64
\t\tsourceColumn: YEAR_NUM

\tpartition Date = m
\t\tmode: import
\t\tsource = ```
\t\t\t\tlet
\t\t\t\t    Source = Snowflake.Databases("acme.snowflakecomputing.com", "ANALYTICS_WH"),
\t\t\t\t    Q = Value.NativeQuery(Source{[Name=DatabaseName]}[Data], "select DATE_KEY, YEAR(DATE_KEY) as YEAR_NUM from SALES.DIM_DATE", null, [EnableFolding=true])
\t\t\t\tin
\t\t\t\t    Q
\t\t\t\t```
"""

TMDL_REL = """relationship a1
\tfromColumn: Sales.CustomerKey
\ttoColumn: Customer.CustomerKey

relationship a2
\tfromColumn: Sales.OrderDate
\ttoColumn: 'Date'.Date
"""

TMDL_EXPR_PROD = 'expression DatabaseName = "PROD_DB" meta [IsParameterQuery=true, Type="Text"]\n'
TMDL_EXPR_TEST = 'expression DatabaseName = "TEST_DB" meta [IsParameterQuery=true, Type="Text"]\n'


def tmdl_parts(env="prod"):
    sales = TMDL_SALES
    if env == "test":
        sales = sales.replace("SUM(Sales[Amount])", "SUM(Sales[Amount]) * 1")
    return {
        "definition/model.tmdl": "model Model\n\tculture: en-US\n",
        "definition/tables/Sales.tmdl": sales,
        "definition/tables/Customer.tmdl": TMDL_CUSTOMER,
        "definition/tables/Date.tmdl": TMDL_DATE,
        "definition/relationships.tmdl": TMDL_REL,
        "definition/expressions.tmdl": TMDL_EXPR_PROD if env == "prod" else TMDL_EXPR_TEST,
    }


def _col(entity, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _mea(entity, prop):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _proj(f, ref):
    return {"field": f, "queryRef": ref, "nativeQueryRef": ref.split(".")[-1]}


REGION_FILTER = {
    "name": "f1", "type": "Categorical", "field": _col("Customer", "Region"),
    "filter": {"Version": 2, "From": [{"Name": "c", "Entity": "Customer", "Type": 0}],
               "Where": [{"Condition": {"In": {
                   "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}},
                                               "Property": "Region"}}],
                   "Values": [[{"Literal": {"Value": "'West'"}}], [{"Literal": {"Value": "'East'"}}]]}}}]}}

AMOUNT_FILTER = {
    "name": "f2", "type": "Advanced", "field": _col("Sales", "Amount"),
    "filter": {"Version": 2, "From": [{"Name": "s", "Entity": "Sales", "Type": 0}],
               "Where": [{"Condition": {"Comparison": {"ComparisonKind": 1,
                   "Left": {"Column": {"Expression": {"SourceRef": {"Source": "s"}},
                                       "Property": "Amount"}},
                   "Right": {"Literal": {"Value": "0D"}}}}}]}}


def pbir_parts():
    table_visual = {
        "name": "v_table",
        "position": {"x": 10, "y": 10, "width": 600, "height": 300},
        "visual": {"visualType": "tableEx",
                   "query": {"queryState": {"Values": {"projections": [
                       _proj(_col("Customer", "Region"), "Customer.Region"),
                       _proj(_mea("Sales", "Total Sales"), "Sales.Total Sales"),
                       _proj(_mea("Sales", "Order Count"), "Sales.Order Count")]}}},
                   "visualContainerObjects": {"title": [{"properties": {"text": {"expr": {
                       "Literal": {"Value": "'Sales by Region'"}}}}}]}},
        "filterConfig": {"filters": [AMOUNT_FILTER]}}
    card = {"name": "v_card", "position": {"x": 620, "y": 10, "width": 200, "height": 120},
            "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
                _proj(_mea("Sales", "Avg Order Value"), "Sales.Avg Order Value")]}}}}}
    chart = {"name": "v_chart", "position": {"x": 10, "y": 320, "width": 600, "height": 300},
             "visual": {"visualType": "clusteredColumnChart", "query": {"queryState": {
                 "Category": {"projections": [{"field": {"HierarchyLevel": {"Expression": {
                     "Hierarchy": {"Expression": {"PropertyVariationSource": {
                         "Expression": {"SourceRef": {"Entity": "Sales"}}, "Name": "Variation",
                         "Property": "OrderDate"}}, "Hierarchy": "Date Hierarchy"}},
                     "Level": "Year"}}, "queryRef": "Sales.OrderDate.Variation.Date Hierarchy.Year"}]},
                 "Y": {"projections": [_proj({"Aggregation": {"Expression": _col("Sales", "Amount"),
                                                              "Function": 0}}, "Sum(Sales.Amount)")]}}}}}
    slicer = {"name": "v_slicer", "position": {"x": 620, "y": 140, "width": 200, "height": 200},
              "visual": {"visualType": "slicer", "query": {"queryState": {"Values": {"projections": [
                  _proj(_col("Customer", "Customer Name"), "Customer.Customer Name")]}}},
                  "objects": {"general": [{"properties": {"filter": {"filter": {
                      "Version": 2, "From": [{"Name": "c", "Entity": "Customer", "Type": 0}],
                      "Where": [{"Condition": {"Not": {"Expression": {"In": {
                          "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}},
                                                      "Property": "Customer Name"}}],
                          "Values": [[{"Literal": {"Value": "'Test Customer'"}}]]}}}}}]}}}}]}}}
    text = {"name": "v_text", "position": {"x": 840, "y": 10, "width": 300, "height": 60},
            "visual": {"visualType": "textbox"}}
    page = {"name": "page1", "displayName": "Overview", "width": 1280, "height": 720,
            "filterConfig": {"filters": [REGION_FILTER]}}
    # Year + Month date slicer (auto date hierarchy) with 2025 selected
    date_slicer = {"name": "v_date", "position": {"x": 840, "y": 80, "width": 300, "height": 120},
                   "visual": {"visualType": "slicer", "query": {"queryState": {"Values": {"projections": [
                       {"field": _date_level("Year"), "queryRef": "Sales.OrderDate.Variation.Date Hierarchy.Year"},
                       {"field": _date_level("Month"), "queryRef": "Sales.OrderDate.Variation.Date Hierarchy.Month"}]}}},
                       "visualContainerObjects": {"title": [{"properties": {"text": {"expr": {
                           "Literal": {"Value": "'Year / Month'"}}}}}]},
                       "objects": {"general": [{"properties": {"filter": {"filter": {
                           "Version": 2, "From": [{"Name": "s", "Entity": "Sales", "Type": 0}],
                           "Where": [{"Condition": {"In": {
                               "Expressions": [_date_level("Year", "s")],
                               "Values": [[{"Literal": {"Value": "2025L"}}]]}}}]}}}}]}}}
    # Two "tab" buttons that apply bookmarks; the detail table is hidden until "Details"
    btn1 = {"name": "btn_summary", "position": {"x": 840, "y": 220, "width": 140, "height": 40},
            "visual": {"visualType": "actionButton", "visualContainerObjects": {"title": [
                {"properties": {"text": {"expr": {"Literal": {"Value": "'Summary tab'"}}}}}],
                "visualLink": [{"properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "type": {"expr": {"Literal": {"Value": "'Bookmark'"}}},
                    "bookmark": {"expr": {"Literal": {"Value": "'bmSummary'"}}}}}]}}}
    btn2 = {"name": "btn_details", "position": {"x": 990, "y": 220, "width": 140, "height": 40},
            "visual": {"visualType": "actionButton", "visualContainerObjects": {"title": [
                {"properties": {"text": {"expr": {"Literal": {"Value": "'Details tab'"}}}}}],
                "visualLink": [{"properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "type": {"expr": {"Literal": {"Value": "'Bookmark'"}}},
                    "bookmark": {"expr": {"Literal": {"Value": "'bmDetails'"}}}}}]}}}
    detail = {"name": "v_detail", "isHidden": True,
              "position": {"x": 10, "y": 10, "width": 600, "height": 300},
              "visual": {"visualType": "tableEx", "query": {"queryState": {"Values": {"projections": [
                  _proj(_col("Customer", "Customer Name"), "Customer.Customer Name"),
                  _proj(_mea("Sales", "Total Sales"), "Sales.Total Sales")]}}},
                  "visualContainerObjects": {"title": [{"properties": {"text": {"expr": {
                      "Literal": {"Value": "'Sales by Customer'"}}}}}]}}}
    parts = {
        "definition/report.json": json.dumps({"filterConfig": {"filters": []}}),
        "definition/pages/pages.json": json.dumps({"pageOrder": ["page1"]}),
        "definition/pages/page1/page.json": json.dumps(page),
        "definition/bookmarks/bookmarks.json": json.dumps({"items": [
            {"name": "grpTabs", "displayName": "Tabs", "children": ["bmSummary", "bmDetails"]}]}),
        "definition/bookmarks/bmSummary.bookmark.json": json.dumps(_bookmark(
            "bmSummary", "Summary", hide=["v_detail"], show=["v_table"])),
        "definition/bookmarks/bmDetails.bookmark.json": json.dumps(_bookmark(
            "bmDetails", "Details", hide=["v_table"], show=["v_detail"])),
    }
    for v in (table_visual, card, chart, slicer, text, date_slicer, btn1, btn2, detail):
        parts[f"definition/pages/page1/visuals/{v['name']}/visual.json"] = json.dumps(v)
    return parts


def _date_level(level, source=None):
    sr = {"Source": source} if source else {"Entity": "Sales"}
    return {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"PropertyVariationSource": {
        "Expression": {"SourceRef": sr}, "Name": "Variation", "Property": "OrderDate"}},
        "Hierarchy": "Date Hierarchy"}}, "Level": level}}


def _bookmark(name, display, hide, show):
    vcs = {v: {"singleVisual": {"display": {"mode": "hidden"}}} for v in hide}
    vcs.update({v: {"singleVisual": {"display": {"mode": "visible"}}} for v in show})
    return {"name": name, "displayName": display,
            "explorationState": {"version": "1.3", "activeSection": "page1",
                                 "sections": {"page1": {"visualContainers": vcs}}},
            "options": {"suppressData": True}}


def legacy_report():
    cfg = {"name": "lv1", "singleVisual": {
        "visualType": "pivotTable",
        "projections": {"Rows": [{"queryRef": "Customer.Region"}],
                        "Values": [{"queryRef": "Sales.Total Sales"}]},
        "prototypeQuery": {"Version": 2, "From": [{"Name": "c", "Entity": "Customer"},
                                                  {"Name": "s", "Entity": "Sales"}],
                           "Select": [
                               {"Column": {"Expression": {"SourceRef": {"Source": "c"}}, "Property": "Region"},
                                "Name": "Customer.Region"},
                               {"Measure": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Total Sales"},
                                "Name": "Sales.Total Sales"}]}}}
    return {"report.json": json.dumps({
        "filters": "[]",
        "sections": [{"name": "ReportSection1", "displayName": "Overview", "ordinal": 0,
                      "filters": json.dumps([REGION_FILTER]),
                      "visualContainers": [{"x": 0, "y": 0, "width": 400, "height": 300,
                                            "config": json.dumps(cfg), "filters": "[]"}]}]})}
