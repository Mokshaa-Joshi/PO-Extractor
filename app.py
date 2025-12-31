import streamlit as st
import pdfplumber
import pandas as pd
import oci
import json
import tempfile
import os
from io import BytesIO

# ================= STREAMLIT PAGE =================
st.set_page_config(page_title="PO-GRN-MRN Extractor", layout="wide")
st.title("📄 PO / GRN / MRN Data Extraction")

# ================= SAFETY CHECK =================
if "oci" not in st.secrets:
    st.error("❌ OCI credentials not found. Please configure Streamlit Secrets.")
    st.stop()

# ================= OCI CONFIG (STREAMLIT CLOUD SAFE) =================
OCI_CONFIG = {
    "user": st.secrets["oci"]["user"],
    "fingerprint": st.secrets["oci"]["fingerprint"],
    "tenancy": st.secrets["oci"]["tenancy"],
    "region": st.secrets["oci"]["region"],
    "key_content": st.secrets["oci"]["private_key"],
}

COMPARTMENT_ID = st.secrets["oci"]["compartment_id"]
MODEL_ID = st.secrets["oci"]["model_id"]
ENDPOINT = st.secrets["oci"]["endpoint"]

client = oci.generative_ai_inference.GenerativeAiInferenceClient(
    config=OCI_CONFIG,
    service_endpoint=ENDPOINT,
    retry_strategy=oci.retry.NoneRetryStrategy(),
    timeout=(10, 240)
)

# ================= PDF TEXT =================
def extract_pdf_text(path):
    text_all = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text()
            if txt:
                text_all += txt + "\n"
    return text_all

# ================= OCI EXTRACTION =================
def extract_structured_data(pdf_text, pdf_type):

    if pdf_type == "PO":
        schema = {
            "header": {
                "CHAIN": "",
                "SITE": "",
                "STATE": "",
                "Vendor Code": "",
                "vendor name": "",
                "po no": "",
                "po date": "",
                "DELIVERY DATE": ""
            },
            "items": [
                {
                    "Material Description": "",
                    "Quantity": "",
                    "total pcs": "",
                    "Base Cost": "",
                    "Total Base Value": ""
                }
            ]
        }

        instructions = """
- Extract item-wise data exactly as visible
- CHAIN is the company name written at the top
- Base Cost MUST come only from the column labeled "Base Cost"
- Do NOT use MRP
"""

    elif pdf_type == "GRN":
        schema = {
            "header": {
                "GRN no": "",
                "GRN Date": ""
            },
            "items": [
                {
                    "Delivered Qty": "",
                    "Remarks": ""
                }
            ]
        }

        instructions = """
VERY IMPORTANT – FOLLOW EXACTLY:

1. Delivered Qty MUST be taken from the column labeled "Accepted Qty / MRP"
2. This column has TWO values:
   - TOP value = Accepted Quantity → USE THIS AS Delivered Qty
   - BOTTOM value = MRP → IGNORE THIS COMPLETELY
3. Remarks MUST be taken from the column labeled:
   "Reason - Short Description"
4. Extract ONE item per table row
5. Do NOT use Challan Qty
6. Do NOT use Received Qty
7. Do NOT use totals
"""

    else:  # MRN
        schema = {
            "header": {
                "MRN no": ""
            },
            "items": [
                {
                    "Rejected Amount": ""
                }
            ]
        }

        instructions = """
- Extract Rejected Amount from MRN item table
- Use the Total Amount for the rejected material
- Do NOT infer or calculate values
"""

    prompt = f"""
You are an expert at reading Indian {pdf_type} PDFs.

{instructions}

Extract data EXACTLY as per the schema.
Return STRICT JSON ONLY.

Schema:
{json.dumps(schema, indent=2)}

PDF Text:
<<<
{pdf_text}
>>>
"""

    chat_request = oci.generative_ai_inference.models.CohereChatRequest()
    chat_request.message = prompt
    chat_request.max_tokens = 4000
    chat_request.temperature = 0

    chat_details = oci.generative_ai_inference.models.ChatDetails()
    chat_details.compartment_id = COMPARTMENT_ID
    chat_details.serving_mode = oci.generative_ai_inference.models.OnDemandServingMode(
        model_id=MODEL_ID
    )
    chat_details.chat_request = chat_request

    response = client.chat(chat_details)
    raw = response.data.chat_response.text

    start = raw.find("{")
    end = raw.rfind("}") + 1

    return json.loads(raw[start:end])

# ================= FILE UPLOAD UI =================
st.subheader("📤 Upload PDFs")

po_file = st.file_uploader("Upload PO PDF", type=["pdf"])
grn_file = st.file_uploader("Upload GRN PDF", type=["pdf"])
mrn_file = st.file_uploader("Upload MRN PDF", type=["pdf"])

if st.button("🚀 Process PDFs"):

    if not (po_file and grn_file and mrn_file):
        st.error("Please upload PO, GRN and MRN PDFs")
        st.stop()

    with tempfile.TemporaryDirectory() as tmpdir:

        po_path = os.path.join(tmpdir, "po.pdf")
        grn_path = os.path.join(tmpdir, "grn.pdf")
        mrn_path = os.path.join(tmpdir, "mrn.pdf")

        for file, path in [
            (po_file, po_path),
            (grn_file, grn_path),
            (mrn_file, mrn_path),
        ]:
            with open(path, "wb") as f:
                f.write(file.read())

        with st.spinner("📄 Processing PO..."):
            po_text = extract_pdf_text(po_path)
            po_data = extract_structured_data(po_text, "PO")

        header = po_data["header"]
        po_items = po_data["items"]

        rows = []
        for item in po_items:
            rows.append({
                **header,
                **item,
                "GRN no": "",
                "GRN Date": "",
                "Delivered Qty": "",
                "Remarks": "",
                "MRN no": "",
                "Rejected Amount": ""
            })

        with st.spinner("📄 Processing GRN..."):
            grn_text = extract_pdf_text(grn_path)
            grn_data = extract_structured_data(grn_text, "GRN")

        for i, item in enumerate(grn_data["items"]):
            if i < len(rows):
                rows[i]["GRN no"] = grn_data["header"]["GRN no"]
                rows[i]["GRN Date"] = grn_data["header"]["GRN Date"]
                rows[i]["Delivered Qty"] = item.get("Delivered Qty", "")
                rows[i]["Remarks"] = item.get("Remarks", "")

        with st.spinner("📄 Processing MRN..."):
            mrn_text = extract_pdf_text(mrn_path)
            mrn_data = extract_structured_data(mrn_text, "MRN")

        for i, item in enumerate(mrn_data["items"]):
            if i < len(rows):
                rows[i]["MRN no"] = mrn_data["header"]["MRN no"]
                rows[i]["Rejected Amount"] = item.get("Rejected Amount", "")

        df = pd.DataFrame(rows)

        st.success("✅ Extraction Completed")
        st.subheader("📊 Extracted Data")
        st.dataframe(df, width="stretch")

        # ================= EXCEL DOWNLOAD =================
        buffer = BytesIO()
        df.to_excel(buffer, index=False, engine="openpyxl")
        buffer.seek(0)

        st.download_button(
            label="⬇️ Download Excel",
            data=buffer,
            file_name="Reliance_PO_GRN_MRN_Extracted.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
