from fastmcp import FastMCP, Context
from pydantic import BaseModel, Field
import os
import io
import json
from dotenv import load_dotenv
from supabase import create_client, Client

# PDF Libraries
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

load_dotenv()

# --------------------------------
# 1. SETUP SUPABASE
# --------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET_NAME = "od-files"  

# Initialize Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

mcp = FastMCP(name="od-pdf-drafter", version="3.0.0")

# --------------------------------
# 2. HELPER: Cloud Operations
# --------------------------------
def download_pdf_from_cloud(filename: str) -> io.BytesIO:
    """Downloads PDF bytes from Supabase."""
    try:
        response = supabase.storage.from_(BUCKET_NAME).download(filename)
        return io.BytesIO(response)
    except Exception:
        return None

def upload_pdf_to_cloud(filename: str, file_data: io.BytesIO) -> str:
    """Uploads PDF and returns the Public URL."""
    # Reset pointer to start of file
    file_data.seek(0)
    
    # Upload (file_options allow overwriting if needed, but we use versioning)
    supabase.storage.from_(BUCKET_NAME).upload(
        path=filename,
        file=file_data.read(),
        file_options={"content-type": "application/pdf"}
    )
    
    # Get Public URL
    public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
    return public_url

def get_next_version_name(quote_number: str) -> str:
    """
    Checks Supabase for existing versions to find the next available name.
    Note: This lists files in the bucket to count versions.
    """
    base_name = f"{quote_number}.pdf"
    
    # List files in bucket that match the quote number
    files = supabase.storage.from_(BUCKET_NAME).list()
    existing_names = [f['name'] for f in files]

    if base_name not in existing_names:
        return None # Original doesn't exist

    version = 1
    while True:
        candidate = f"{quote_number}_v{version}.pdf"
        if candidate not in existing_names:
            return candidate
        version += 1

# --------------------------------
# 3. HELPER: PDF Generation (Same as before)
# --------------------------------
def create_clause_page(clauses: list[tuple[str, str]]) -> io.BytesIO:
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=letter)
    text_object = can.beginText(40, 750)
    text_object.setFont("Helvetica-Bold", 14)
    text_object.textLine("Addendum: Additional Clauses")
    text_object.moveCursor(0, 20)
    
    for title, description in clauses:
        text_object.setFont("Helvetica-Bold", 12)
        text_object.textLine(f"{title}:")
        text_object.setFont("Helvetica", 10)
        
        words = description.split()
        line = ""
        for word in words:
            if len(line) + len(word) > 80:
                text_object.textLine(line)
                line = ""
            line += word + " "
        text_object.textLine(line)
        text_object.moveCursor(0, 15)

    can.drawText(text_object)
    can.save()
    packet.seek(0)
    return packet

# Load clauses from local file (keep clauses.json in repo)
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CLAUSES_FILE = os.path.join(DATA_DIR, "clauses.json")
def load_clauses():
    if not os.path.exists(CLAUSES_FILE): return {}
    with open(CLAUSES_FILE, "r") as f: return json.load(f)

# --------------------------------
# 4. THE TOOL
# --------------------------------
@mcp.tool(
    name="draft_pdf_od",
    description="Append clauses to a PDF quote stored in Supabase Cloud.",
)
async def draft_pdf_od(ctx: Context, quote_number: str, user_query: str):
    
    # A. Check original file in Cloud
    original_filename = f"{quote_number}.pdf"
    original_file_stream = download_pdf_from_cloud(original_filename)
    
    if not original_file_stream:
        return {
            "content": [{"type": "text", "text": f"❌ Could not find '{original_filename}' in Supabase bucket '{BUCKET_NAME}'."}],
            "isError": True
        }

    # B. Identify Clauses
    clause_db = load_clauses()
    available = list(clause_db.keys())
    matched = [t for t in available if t.lower() in user_query.lower()]
    
    if not matched:
        # Simple elicitation fallback
        return {"content": [{"type": "text", "text": f"❌ No clauses found in query. Available: {', '.join(available)}"}]}
    
    clauses_to_add = [(t, clause_db[t]) for t in matched]

    try:
        # C. Merge PDFs
        new_page = PdfReader(create_clause_page(clauses_to_add))
        existing_pdf = PdfReader(original_file_stream)
        output = PdfWriter()

        for page in existing_pdf.pages:
            output.add_page(page)
        output.add_page(new_page.pages[0])

        # D. Save to Stream
        final_stream = io.BytesIO()
        output.write(final_stream)
        
        # E. Upload Version to Cloud
        new_filename = get_next_version_name(quote_number)
        public_url = upload_pdf_to_cloud(new_filename, final_stream)

        return {
            "content": [
                {
                    "type": "text", 
                    "text": f"✅ Success! Version '{new_filename}' created."
                },
                {
                    "type": "text",
                    "text": f"📄 View/Download here: {public_url}"
                }
            ]
        }

    except Exception as e:
        return {"content": [{"type": "text", "text": f"❌ Error: {str(e)}"}], "isError": True}

if __name__ == "__main__":
    mcp.run(transport="http", port=8000)