from fastmcp import FastMCP, Context
import os
import io
import json
from dotenv import load_dotenv
from supabase import create_client, Client

# PDF Handling
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from typing import Annotated

load_dotenv()

# --------------------------------
# 1. SETUP SUPABASE
# --------------------------------
# Ensure these are set in your Render Environment Variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") # Your Publishable Key
BUCKET_NAME = "od-files"

# Initialize Client
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"⚠️  Supabase Init Error: {e}")

mcp = FastMCP(name="od-pdf-drafter", version="3.1.0")

# --------------------------------
# 2. CLOUD HELPERS
# --------------------------------

def download_pdf_from_cloud(filename: str) -> io.BytesIO:
    """
    Downloads PDF bytes from Supabase.
    Requires 'SELECT' policy on the bucket.
    """
    try:
        # download() returns bytes in recent SDKs
        response = supabase.storage.from_(BUCKET_NAME).download(filename)
        return io.BytesIO(response)
    except Exception as e:
        print(f"❌ Download failed for {filename}: {e}")
        return None

def upload_pdf_to_cloud(filename: str, file_data: io.BytesIO) -> str:
    """
    Uploads PDF and returns the Public URL.
    Requires 'INSERT' policy on the bucket.
    """
    file_data.seek(0)
    try:
        # We use upsert=false to prevent accidental overwrites, 
        # relying on our versioning logic instead.
        supabase.storage.from_(BUCKET_NAME).upload(
            path=filename,
            file=file_data.read(),
            file_options={"content-type": "application/pdf", "upsert": "false"}
        )
        # Get Public URL for easy viewing
        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
        return public_url
    except Exception as e:
        raise Exception(f"Upload failed. Check your 'INSERT' Policy. Error: {e}")

def get_next_version_name(quote_number: str) -> str:
    """
    Lists files to determine the next version number (v1, v2, v3).
    Requires 'SELECT' policy on the bucket.
    """
    base_name = f"{quote_number}.pdf"
    
    try:
        # List all files in the bucket
        files = supabase.storage.from_(BUCKET_NAME).list()
        existing_names = [f['name'] for f in files]

        # If original doesn't exist, we can't make a version of it
        if base_name not in existing_names:
            print(f"⚠️ Original {base_name} not found in bucket list.")
            return None 

        # Find next available version
        version = 1
        while True:
            candidate = f"{quote_number}_v{version}.pdf"
            if candidate not in existing_names:
                return candidate
            version += 1
            
    except Exception as e:
        print(f"❌ List files failed: {e}")
        # Fallback: If listing fails (strict policy?), try blind creation of v1
        return f"{quote_number}_v1.pdf"

# --------------------------------
# 3. PDF GENERATION LOGIC
# --------------------------------

def create_clause_page(clauses: list[tuple[str, str]]) -> io.BytesIO:
    """Generates a new PDF page with the clauses."""
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=letter)
    
    # Title
    text_object = can.beginText(40, 750)
    text_object.setFont("Helvetica-Bold", 14)
    text_object.textLine("Addendum: Additional Clauses")
    text_object.moveCursor(0, 20)
    
    for title, description in clauses:
        # Clause Title
        text_object.setFont("Helvetica-Bold", 12)
        text_object.textLine(f"{title}:")
        
        # Clause Body
        text_object.setFont("Helvetica", 10)
        
        # Basic word wrap logic
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

# Load Clause Data
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CLAUSES_FILE = os.path.join(DATA_DIR, "clauses.json")

def load_clauses():
    if not os.path.exists(CLAUSES_FILE): return {}
    with open(CLAUSES_FILE, "r") as f: return json.load(f)

# --------------------------------
# 4. MCP TOOL
# --------------------------------

@mcp.tool(
    name="draft_pdf_od",
    description="Append clauses to a PDF Quote stored in Supabase. Handles versioning (v1, v2).",
)
async def draft_pdf_od(ctx: Context, 
                       quote_number: Annotated[str, "The ID of the quote example 12345"], 
                       clause_name: Annotated[str, "Name of the clause to add example Auto Renewal, Usage Rights"]):
    """
    Args:
        quote_number: The ID of the quote (e.g. "100")
        clause_name: The name of the clause to add (e.g. "Auto Renewal")
    """
    
    # --- STEP 1: DOWNLOAD ORIGINAL ---
    original_filename = f"{quote_number}.pdf"
    await ctx.info(f"Downloading {original_filename} from Supabase...")
    
    original_file_stream = download_pdf_from_cloud(original_filename)
    
    if not original_file_stream:
        return {
            "content": [{
                "type": "text", 
                "text": f"❌ Error: Could not download '{original_filename}'. \n1. Check if file exists in bucket '{BUCKET_NAME}'.\n2. Check if your Policy allows 'SELECT' for public/anon key."
            }],
            "isError": True
        }

    # --- STEP 2: IDENTIFY CLAUSES ---
    clause_db = load_clauses()
    available_titles = list(clause_db.keys())
    matched = [t for t in available_titles if t.lower() in clause_name.lower()]
    
    if not matched:
        return {
            "content": [{"type": "text", "text": f"❌ No matching clauses found in query. Available options: {', '.join(available_titles)}"}]
        }
    
    clauses_to_add = [(t, clause_db[t]) for t in matched]

    # --- STEP 3: MERGE PDF ---
    try:
        # Create the new addendum page
        new_page_packet = create_clause_page(clauses_to_add)
        new_page_reader = PdfReader(new_page_packet)
        
        # Read the original downloaded PDF
        existing_pdf = PdfReader(original_file_stream)
        writer = PdfWriter()

        # Add all old pages
        for page in existing_pdf.pages:
            writer.add_page(page)
            
        # Add the new addendum page
        writer.add_page(new_page_reader.pages[0])

        # Save to buffer
        final_output_stream = io.BytesIO()
        writer.write(final_output_stream)
        
        # --- STEP 4: UPLOAD NEW VERSION ---
        new_filename = get_next_version_name(quote_number)
        
        if not new_filename:
             # Should rarely happen unless list() failed but download() worked
             new_filename = f"{quote_number}_v_new.pdf"

        await ctx.info(f"Uploading new version: {new_filename}...")
        public_url = upload_pdf_to_cloud(new_filename, final_output_stream)

        return {
            "content": [
                {
                    "type": "text", 
                    "text": f"✅ Success! Created version '{new_filename}' with clauses: {', '.join(matched)}"
                },
                {
                    "type": "text",
                    "text": f"📄 View updated PDF: {public_url}"
                }
            ]
        }

    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"❌ Processing Error: {str(e)}"}],
            "isError": True
        }

if __name__ == "__main__":
    mcp.run(transport="http", port=8000)
