from fastmcp import FastMCP, Context
from pydantic import BaseModel, Field
import json
import os
import io
import re
from dotenv import load_dotenv

# PDF Libraries
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch

load_dotenv()

# Initialize Server
mcp = FastMCP(name="od-pdf-drafter", version="2.0.0")

# --------------------------------
# Configuration
# --------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CLAUSES_FILE = os.path.join(DATA_DIR, "clauses.json")
os.makedirs(DATA_DIR, exist_ok=True)

# --------------------------------
# Helper: Versioning
# --------------------------------
def get_next_version_path(quote_number: str) -> str:
    """
    Determines the next filename version.
    Example: If '12345.pdf' and '12345_v1.pdf' exist, returns path for '12345_v2.pdf'.
    """
    base_name = f"{quote_number}.pdf"
    base_path = os.path.join(DATA_DIR, base_name)
    
    # If the base file doesn't exist, we can't draft an update
    if not os.path.exists(base_path):
        return None

    # Check for existing versions
    version = 1
    while True:
        candidate_name = f"{quote_number}_v{version}.pdf"
        candidate_path = os.path.join(DATA_DIR, candidate_name)
        if not os.path.exists(candidate_path):
            return candidate_path
        version += 1

def get_latest_pdf_path(quote_number: str) -> str:
    """
    Finds the most recent version of the PDF to build upon.
    (Optional: Currently I assume we always build upon the Original, 
     but you can change this to build upon the latest version if needed).
    """
    return os.path.join(DATA_DIR, f"{quote_number}.pdf")

# --------------------------------
# Helper: PDF Generation
# --------------------------------
def create_clause_page(clauses: list[tuple[str, str]]) -> io.BytesIO:
    """
    Uses ReportLab to generate a single PDF page containing the clauses.
    Returns a bytes buffer.
    """
    packet = io.BytesIO()
    # Create a new PDF with Reportlab
    can = canvas.Canvas(packet, pagesize=letter)
    width, height = letter
    
    # Start writing text from top-left
    text_object = can.beginText(1 * inch, height - 1 * inch)
    text_object.setFont("Helvetica-Bold", 14)
    text_object.textLine("Addendum: Additional Clauses")
    text_object.moveCursor(0, 20) # Add spacing
    
    for title, description in clauses:
        # Title
        text_object.setFont("Helvetica-Bold", 12)
        text_object.textLine(f"{title}")
        
        # Description (Simple wrapping logic handled by reportlab text object usually requires Paragraph flowables, 
        # but for simplicity we will just assume short text or split lines manually. 
        # For production, use ReportLab Platypus Paragraphs for auto-wrapping).
        text_object.setFont("Helvetica", 10)
        
        # Simple word wrap simulation for this demo
        words = description.split()
        line = ""
        for word in words:
            if len(line) + len(word) > 80: # approx characters per line
                text_object.textLine(line)
                line = ""
            line += word + " "
        text_object.textLine(line)
        text_object.moveCursor(0, 15) # Space between clauses

    can.drawText(text_object)
    can.save()
    
    packet.seek(0)
    return packet

# --------------------------------
# Helper: Clause Logic
# --------------------------------
def load_clauses() -> dict[str, str]:
    if not os.path.exists(CLAUSES_FILE): return {}
    with open(CLAUSES_FILE, "r", encoding="utf-8") as f: return json.load(f)

# --------------------------------
# The Tool
# --------------------------------
@mcp.tool(
    name="draft_pdf_od",
    description="Append clauses to an existing PDF Quote. Handles versioning automatically.",
)
async def draft_pdf_od(ctx: Context, quote_number: str, user_query: str):
    """
    Args:
        quote_number: The ID of the file (e.g. "Q-100")
        user_query: The user's request (e.g. "Add Auto Renewal")
    """

    # 1. Validate File Existence
    original_path = os.path.join(DATA_DIR, f"{quote_number}.pdf")
    if not os.path.exists(original_path):
        return {
            "content": [{"type": "text", "text": f"❌ File not found: {original_path}"}],
            "isError": True
        }

    # 2. Identify Clauses (Logic from previous steps)
    clause_db = load_clauses()
    available_titles = list(clause_db.keys())
    matched_titles = [t for t in available_titles if t.lower() in user_query.lower()]

    # --- AMBIGUITY CHECK ---
    if not matched_titles:
        options_str = ", ".join(available_titles)
        class ClauseSelection(BaseModel):
            selected_clause: str = Field(..., description=f"Choose one: {options_str}")

        await ctx.log.info(f"Ambiguity detected for quote {quote_number}")
        result = await ctx.elicit(
            message=f"I found Quote {quote_number}, but I didn't recognize a clause in '{user_query}'.\nOptions: {options_str}",
            response_type=ClauseSelection
        )
        if result.action != "accept":
            return {"content": [{"type": "text", "text": "🚫 Cancelled."}]}
        
        # Validate selection
        sel = result.data.selected_clause
        found = next((t for t in available_titles if t.lower() == sel.lower()), None)
        if not found: return {"content": [{"type": "text", "text": "❌ Invalid clause."}]}
        matched_titles = [found]

    # Prepare data for PDF generation
    clauses_to_add = [(title, clause_db[title]) for title in matched_titles]

    # 3. Process PDF (Merge Logic)
    try:
        # A. Create the new page with clauses
        new_page_packet = create_clause_page(clauses_to_add)
        new_page_pdf = PdfReader(new_page_packet)

        # B. Read the original PDF
        # Note: Depending on requirements, we either load the Original OR the latest Version
        # Here we load the Original base file.
        existing_pdf = PdfReader(original_path)
        output = PdfWriter()

        # Add all existing pages
        for page in existing_pdf.pages:
            output.add_page(page)

        # Add the new "Clauses" page
        output.add_page(new_page_pdf.pages[0])

        # 4. Save Version
        output_filename = get_next_version_path(quote_number)
        with open(output_filename, "wb") as f:
            output.write(f)

        new_filename = os.path.basename(output_filename)
        
        return {
            "content": [
                {
                    "type": "text", 
                    "text": f"✅ Success! Created new version: {new_filename}\n\nAdded Clauses:\n" + ", ".join(matched_titles)
                }
            ]
        }

    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"❌ PDF Processing Error: {str(e)}"}],
            "isError": True
        }

if __name__ == "__main__":
    mcp.run(transport="http", port=8000)