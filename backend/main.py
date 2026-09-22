"""
CaseTwin FastAPI backend.
POST /search   — upload a chest X-ray image, get back similar cases from Qdrant.
POST /extract  — extract a structured CaseProfile from images + clinical notes (mock).
GET  /health   — health check.
"""

import os
from dotenv import load_dotenv
load_dotenv()

import io
import json
import re
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

from embedding_service import generate_embedding, query_medgemma
from qdrant_service import search_similar
from manifest import asset_path
from document_text import DocumentExtractionError, extract_document_text

app = FastAPI(title="CaseTwin API", version="1.0.0")

# Allow the Vite dev server (and any localhost port) to call the API
_configured_origins = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173", "http://localhost:8080", "http://127.0.0.1:8080", *_configured_origins],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/dataset-images/{asset_id}")
def dataset_image(asset_id: str):
    """Serve a derived asset by safe identifier; source paths never leave the backend."""
    try:
        path = asset_path(asset_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Dataset image not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Dataset image not found")
    return FileResponse(path)


def _dataset_url(request: Request, asset_id: str | None) -> str | None:
    return str(request.url_for("dataset_image", asset_id=asset_id)) if asset_id else None


@app.post("/search")
async def search(
    request: Request,
    file: UploadFile = File(...),
    profile: Optional[str] = Form(None),
    limit: int = 5
):
    """
    Accept a chest X-ray image, generate a MedSiglip embedding,
    and return the top `limit` similar cases from Qdrant, re-ranked
    using the extracted CaseProfile.
    """
    if file.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(status_code=400, detail="Only image files are accepted (jpg, png, webp).")

    try:
        contents = await file.read()
        image = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read image: {e}")

    parsed_profile = None
    if profile:
        try:
            parsed_profile = json.loads(profile)
        except Exception as e:
            print(f"Warning: Failed to parse profile JSON: {e}")

    try:
        embedding = generate_embedding(image)
    except Exception as e:
        err_str = str(e)
        if "503" in err_str or "Service Unavailable" in err_str:
            raise HTTPException(
                status_code=503, 
                detail="The AI image matching model is currently waking up or unavailable. Please try again in about 1-2 minutes."
            )
        raise HTTPException(status_code=500, detail=f"Embedding generation failed: {e}")

    try:
        matches = search_similar(embedding, profile_data=parsed_profile, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Qdrant search failed: {e}")

    for match in matches:
        match["image_url"] = _dataset_url(request, match.get("asset_id"))
        match["related_image_urls"] = [_dataset_url(request, asset_id) for asset_id in match.get("related_asset_ids", [])]
        payload = match.get("raw_payload")
        if isinstance(payload, dict):
            # The modal reads the canonical profile, so expose local URLs there too.
            payload.get("study", {})["image_url"] = match["image_url"]
            for related, url in zip(payload.get("related_images", []), match["related_image_urls"]):
                if isinstance(related, dict):
                    related["image_url"] = url
    return {"matches": matches, "count": len(matches)}


# ──────────────────────────────────────────────────────────────────────────────
# /compare_insights  – Use MedGemma to compare abnormalities
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/compare_insights")
async def compare_insights(
    original_image: UploadFile = File(...),
    match_diagnosis: str = Form(...),
    match_asset_id: str = Form(None),
    match_payload: str = Form(None)
):
    """
    Given the original uploaded image and the diagnosis of the matched case,
    ask MedGemma to find bounding boxes for that diagnosis in the original image.
    This also handles the matched image if we pass it, but for simplicity
    we'll fetch/analyze both or simulate bounding boxes if it fails.
    """
    
    # Read original image
    try:
        contents = await original_image.read()
        orig_pil = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read original image: {e}")
        
    # Read matched image from the derived read-only dataset, never from a public URL.
    match_pil = None
    if match_asset_id:
        try:
            local_asset = asset_path(match_asset_id)
            if not local_asset.is_file():
                raise HTTPException(status_code=404, detail="Matched dataset image was not found")
            match_pil = Image.open(local_asset).convert("RGB")
        except Exception as e:
            if isinstance(e, HTTPException):
                raise
            raise HTTPException(status_code=400, detail="Invalid matched dataset image") from e

    # Parse match payload for context
    parsed_payload = {}
    if match_payload:
        try:
            parsed_payload = json.loads(match_payload)
        except Exception as e:
            print(f"Warning: failed to parse match_payload JSON: {e}")
            
    # Query MedGemma for bounding boxes for original
    prompt = f"Return the bounding box coordinates [ymin, xmin, ymax, xmax] for the finding '{match_diagnosis}' in this chest X-ray."
    
    orig_box = None
    match_box = None
    
    # Helper to parse MedGemma [y1, x1, y2, x2] response strings
    def parse_box(text):
        m = re.search(r'\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]', text)
        if m:
            return [int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))]
        return None
        
    # Helper to describe the box position
    def get_region_text(box):
        if not box: return "an unspecified region"
        y1, x1, y2, x2 = box
        xc = (x1 + x2) / 2
        yc = (y1 + y2) / 2
        
        if yc < 333:
            v = "upper"
        elif yc < 666:
            v = "mid"
        else:
            v = "lower"
            
        if xc < 333:
            h = "left"
        elif xc < 666:
            h = "central"
        else:
            h = "right"
            
        return f"{v} {h} region"
        
    # Query for original image box
    try:
        resp = query_medgemma(orig_pil, prompt=prompt, max_tokens=50)
        if isinstance(resp, list) and len(resp) > 0:
            box_text = resp[0].get("generated_text", "")
            orig_box = parse_box(box_text)
    except Exception as e:
        print(f"MedGemma orig box extraction error: {e}")
        
    # Query for match image
    if match_pil:
        try:
            resp = query_medgemma(match_pil, prompt=prompt, max_tokens=50)
            if isinstance(resp, list) and len(resp) > 0:
                box_text = resp[0].get("generated_text", "")
                match_box = parse_box(box_text)
        except Exception as e:
            print(f"MedGemma match box extraction error: {e}")
            
    # Do not fabricate boxes. A model that cannot localize a finding reports no box.
    if not orig_box or not match_box:
        raise HTTPException(status_code=422, detail="The local model did not return localization boxes for this comparison.")

    orig_region = get_region_text(orig_box) if orig_box else "the affected region"
    match_region = get_region_text(match_box) if match_box else "the affected region"
    
    import re

    # Build a tight prompt that forces a single, concise, non-repeating output
    hpi = parsed_payload.get("presentation", {}).get("hpi", "")
    outcome = parsed_payload.get("outcome", {}).get("detail", "")

    unified_prompt = (
        f"You are a radiology AI assistant. Analyze this chest X-ray for suspected '{match_diagnosis}'. "
        f"The primary finding in the current image is in the {orig_region}. "
        f"The historical twin case had primary involvement in the {match_region}. "
        f"Clinical history: {hpi or 'not provided'}. Historical outcome: {outcome or 'not provided'}. "
        f"Write exactly 5-6 sentences. Cover: (1) what the current finding looks like, "
        f"(2) why the highlighted region is clinically significant, "
        f"(3) how it visually compares to the historical case, "
        f"(4) what this similarity suggests prognostically. "
        f"Use **bold** for key medical terms. Do NOT repeat yourself. Stop after 6 sentences."
    )

    try:
        import asyncio
        resp = await asyncio.to_thread(query_medgemma, orig_pil, prompt=unified_prompt, max_tokens=400)
        gen_text = "AI analysis unavailable."
        if isinstance(resp, list) and len(resp) > 0 and resp[0].get("generated_text"):
            raw = resp[0]["generated_text"].strip()
            # Strip prompt echo if model returns the full prompt+completion
            if raw.startswith(unified_prompt):
                raw = raw[len(unified_prompt):].strip()
            # Remove any leading "markdown" / code fence artifacts
            raw = re.sub(r"^```(?:markdown)?\s*", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"```$", "", raw).strip()
            # Strip LaTeX boxed notation the model sometimes wraps output in
            # e.g.  $\boxed{The current image shows...}$  or  \boxed{...}
            raw = re.sub(r"\$?\\?boxed\{(.+?)\}\$?", r"\1", raw, flags=re.DOTALL)
            raw = raw.strip()
            # Deduplicate: if the model loops, keep only the first unique occurrence
            # Split on common sentence-repeat markers
            seen = set()
            sentences = re.split(r"(?<=[.!?])\s+", raw)
            deduped = []
            for s in sentences:
                key = s.strip().lower()[:60]
                if key not in seen:
                    seen.add(key)
                    deduped.append(s.strip())
                # Stop after 6 sentences
                if len(deduped) >= 6:
                    break
            gen_text = " ".join(deduped)

    except Exception as e:
        print(f"MedGemma unified extraction error: {e}")
        gen_text = "Unable to complete AI analysis at this time."

    return {
        "insights_text": gen_text,
        "original_box": orig_box,
        "match_box": match_box,
    }


# ──────────────────────────────────────────────────────────────────────────────
# /search_hospitals  – You.com RAG integration for dynamic facility routing
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/search_hospitals")
async def search_hospitals(
    diagnosis: str = Form(...),
    location: Optional[str] = Form(None),
    equipment: Optional[str] = Form(None),
    maxTravelTime: Optional[str] = Form(None),
    maxDistance: Optional[str] = Form(None)
):
    """
    Query the You.com RAG API to find relevant top-tier hospitals for the given diagnosis.
    Returns structured data detailing facility names, capabilities, reason for match,
    and approximate locations/coordinates for mapping.
    """
    import os
    import httpx
    import json
    
    ydc_api_key = os.getenv("YDC_API_KEY")
    if not ydc_api_key:
        raise HTTPException(status_code=500, detail="YDC_API_KEY environment variable is missing.")

    loc_context = f" near {location}" if location else " in the United States"
    eq_context = f" YOU MUST ONLY INCLUDE HOSPITALS THAT EXPLICITLY HAVE THE FOLLOWING EQUIPMENT/CAPABILITIES: {equipment}." if equipment else ""
    travel_context = f" The hospital MUST be reachable within a {maxTravelTime} hour travel time from the location." if maxTravelTime else ""
    
    # Use Geopy for real coordinates and OSRM for real routing
    from geopy.geocoders import Nominatim
    import asyncio
    
    # Using a custom user agent as required by Nominatim's Terms of Service
    geolocator = Nominatim(user_agent="casetwin_medical_routing_bot")
    
    user_lat, user_lng = 39.8283, -98.5795 # Default US Center
    search_location_str = location or 'United States'
    
    if location:
        # Check if location is coordinates (e.g., "28.5383, -81.3792")
        is_coords = False
        if ',' in location:
            parts = location.split(',')
            try:
                user_lat = float(parts[0].strip())
                user_lng = float(parts[1].strip())
                is_coords = True
            except ValueError:
                pass
                
        if is_coords:
            def reverse_loc(lat, lng):
                return geolocator.reverse(f"{lat}, {lng}", timeout=5)
            try:
                rev_data = await asyncio.to_thread(reverse_loc, user_lat, user_lng)
                if rev_data:
                    address = rev_data.raw.get('address', {})
                    city = address.get('city') or address.get('town') or address.get('county') or address.get('state')
                    if city:
                        search_location_str = f"{city}, {address.get('state', '')}"
                        print(f"Reverse geocode success: {location} -> {search_location_str}", flush=True)
            except Exception as e:
                print(f"Reverse geocode failed: {e}", flush=True)
        else:
            def geocode_loc(loc_str):
                return geolocator.geocode(loc_str, timeout=5)
            try:
                user_loc_data = await asyncio.to_thread(geocode_loc, location)
                if user_loc_data:
                    user_lat, user_lng = user_loc_data.latitude, user_loc_data.longitude
            except Exception as e:
                print(f"Warning: Geocoding user location '{location}' failed: {e}", flush=True)

    distance_context = f" within {maxDistance} miles" if maxDistance else ""
    query = f"top hospitals medical centers {search_location_str}{distance_context} treating {diagnosis} {equipment or ''}"

    headers = {
        "X-API-Key": ydc_api_key,
    }
    
    payload = {
        "query": query,
        "count": 10
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get("https://ydc-index.io/v1/search", headers=headers, params=payload)
            resp.raise_for_status()
            data = resp.json()
            
            # Extract standard web search results
            web_results = data.get("results", {}).get("web", [])
            all_text = ""
            for hit in web_results:
                snippets = hit.get("snippets", [])
                if snippets:
                    all_text += " ".join(snippets) + "\n"
            
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [search_hospitals] You.com Search Snippets:\n{all_text[:300]}...\n", flush=True)

            import random
            
            centers = []
            seen_names = set()
            
            for i, hit in enumerate(web_results):
                if len(centers) >= 10:
                    break
                    
                title = hit.get("title", f"Top Hospital {len(centers)+1}")
                url = hit.get("url", "")
                
                import re
                
                # --- Smarter Name Extraction ---
                name = title.split(" | ")[0].split(" - ")[0].strip()
                
                # If name is extremely generic (e.g. just a department name), use the URL domain instead
                generic_terms = ["interventional", "radiology", "imaging", "mri", "ct", "paragonimiasis", "services", "treatment", "clinic"]
                if any(term in name.lower() for term in generic_terms):
                    try:
                        import urllib.parse
                        domain = urllib.parse.urlparse(url).netloc
                        clean_domain = domain.replace("www.", "").split(".")[0]
                        
                        # Add spaces before common medical words to beautify squished names
                        # e.g., americanhealthimaging -> american health imaging
                        spaced_name = re.sub(
                            r'(american|national|regional|state|county|city|health|imaging|medical|care|hospital|clinic|center|florida|new|york|texas|memorial|university|mount|sinai|ny|nyp|nsuh|tmh|general|childrens|cancer|institute|pediatric)', 
                            r' \1 ', clean_domain, flags=re.IGNORECASE
                        )
                        # Clean up any double spaces and title case it
                        spaced_name = " ".join(spaced_name.split()).title()
                        
                        name = spaced_name + " Hospital"
                    except:
                        pass
                
                name = name.replace("...", "").strip()
                if not name:
                    name = f"Medical Center {len(centers)+1}"
                    
                # Ensure the name is unique to prevent duplicate React keys
                if name.lower() in seen_names:
                    continue
                seen_names.add(name.lower())
                
                # --- Geopy Coordinates ---
                # Fallback fuzz is tightly clustered around the requested city (user_lat/user_lng)
                h_lat = user_lat + random.uniform(-0.06, 0.06)
                h_lng = user_lng + random.uniform(-0.06, 0.06)
                
                try:
                    # Append the search_location_str to give Nominatim geographic context
                    # Remove the word 'Hospital' if it was injected, as it confuses Geopy sometimes
                    clean_query_name = name.replace(" Hospital", "")
                    geo_query = f"{clean_query_name}, {search_location_str}"
                    h_loc_data = await asyncio.to_thread(geocode_loc, geo_query)
                    if h_loc_data:
                        h_lat, h_lng = h_loc_data.latitude, h_loc_data.longitude
                    else:
                        # Try just the name without ' hospital' but with location
                        h_loc_data_fallback = await asyncio.to_thread(geocode_loc, name)
                        if h_loc_data_fallback:
                            h_lat, h_lng = h_loc_data_fallback.latitude, h_loc_data_fallback.longitude
                except Exception as e:
                    print(f"Geocoding hospital '{name}' failed: {e}", flush=True)
                
                # --- OSRM ETA Calculation ---
                travel_str = f"{1 + i}h {(i * 15) % 60}m" # Fallback mock time
                try:
                    # OSRM expects coordinates in lng,lat order
                    osrm_url = f"http://router.project-osrm.org/route/v1/driving/{user_lng},{user_lat};{h_lng},{h_lat}?overview=false"
                    osrm_resp = await client.get(osrm_url)
                    if osrm_resp.status_code == 200:
                        route_data = osrm_resp.json()
                        if route_data.get("routes") and len(route_data["routes"]) > 0:
                            duration_seconds = route_data["routes"][0].get("duration", 0)
                            hours = int(duration_seconds // 3600)
                            minutes = int((duration_seconds % 3600) // 60)
                            if hours > 0:
                                travel_str = f"{hours}h {minutes}m"
                            else:
                                travel_str = f"{minutes}m"
                            print(f"[OSRM] Calculated true driving ETA for '{name}': {travel_str} (Distance: {round(route_data['routes'][0].get('distance',0)*0.000621371, 1)} miles)", flush=True)
                except Exception as e:
                    print(f"OSRM ETA failed for {name}: {e}", flush=True)

                # Construct the full reason from description or snippets without aggressively truncating
                raw_desc = hit.get("description", "")
                if not raw_desc:
                    raw_desc = " ".join(hit.get("snippets", []))
                
                if not raw_desc:
                    raw_desc = "Specialized care facility."
                
                # Still cap it at a reasonable length to prevent massive text blocks, but much larger than 60 chars
                final_reason = raw_desc[:350] + ("..." if len(raw_desc) > 350 else "")

                centers.append({
                    "name": name,
                    "url": url,
                    "capability": str(99 - i) + "%",
                    "travel": travel_str,
                    "reason": final_reason,
                    "lat": h_lat,
                    "lng": h_lng
                })
            
            if centers:
                return {"centers": centers}
            else:
                raise ValueError("No results found in You.com Search")
            
    except Exception as e:
        print(f"Failed to fetch or parse You.com data: {e}")
        import traceback
        traceback.print_exc()
        # Fallback to simulated data if the API request or JSON parsing fails
        fallback = [
             {
                "name": "Mayo Clinic — Rochester",
                "url": "https://www.mayoclinic.org/patient-visitor-guide/minnesota",
                "capability": "100%",
                "travel": "2h 10m",
                "reason": f"Interventional Pulmonology + Leading care for {diagnosis}",
                "lat": 44.0227,
                "lng": -92.4667
            },
            {
                "name": "Cleveland Clinic",
                "url": "https://my.clevelandclinic.org/locations",
                "capability": "95%",
                "travel": "1h 55m",
                "reason": "Thoracic surgery + Clinical trials",
                "lat": 41.5034,
                "lng": -81.6206
            },
            {
                "name": "Mass General",
                "url": "https://www.massgeneral.org/",
                "capability": "90%",
                "travel": "3h 05m",
                "reason": "Radiation oncology + Research program",
                "lat": 42.3621,
                "lng": -71.0691
            }
        ]
        return {"centers": fallback}


# ──────────────────────────────────────────────────────────────────────────────
# /chat_twin  – Use MedGemma to answer questions about a case twin
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/chat_twin")
async def chat_twin(
    query: str = Form(...),
    case_text: str = Form(...),
    current_profile: Optional[str] = Form(default=None),
):
    """
    Dual-context clinical reasoning. Grounds MedGemma in:
      1. The historical twin case (case_text + any structured payload)
      2. The current patient's CaseProfile (current_profile JSON, optional)
    Returns a markdown-formatted reply.
    """
    # ── Build current patient context block ──────────────────────────────────
    current_ctx = ""
    if current_profile:
        try:
            cp = json.loads(current_profile)
            pat = cp.get("patient", {})
            pres = cp.get("presentation", {})
            assess = cp.get("assessment", {})
            findings = cp.get("findings", {})

            age = pat.get("age_years")
            sex = pat.get("sex")
            comorbidities = ", ".join(pat.get("comorbidities", [])) or "none documented"
            cc = pres.get("chief_complaint") or "not specified"
            hpi = (pres.get("hpi") or "")[:300]
            diagnosis = assess.get("diagnosis_primary") or "undetermined"
            urgency = assess.get("urgency") or ""
            icu = assess.get("icu_candidate") or ""

            lung_findings = []
            lungs = findings.get("lungs", {})
            pleura = findings.get("pleura", {})
            cardio = findings.get("cardiomediastinal", {})
            if lungs.get("consolidation_present") == "yes": lung_findings.append("consolidation")
            if lungs.get("edema_present") == "yes": lung_findings.append("pulmonary edema")
            if lungs.get("atelectasis_present") == "yes": lung_findings.append("atelectasis")
            if pleura.get("effusion_present") == "yes": lung_findings.append("pleural effusion")
            if pleura.get("pneumothorax_present") == "yes": lung_findings.append("pneumothorax")
            if cardio.get("cardiomegaly") == "yes": lung_findings.append("cardiomegaly")

            findings_str = ", ".join(lung_findings) if lung_findings else "none extracted"

            current_ctx = f"""
## Current Patient Profile
- **Demographics:** {f'{age}y ' if age else ''}{sex or 'unknown sex'}
- **Comorbidities:** {comorbidities}
- **Chief complaint:** {cc}
- **Clinical narrative:** {hpi or 'not provided'}
- **Primary diagnosis (extracted):** {diagnosis}
- **Urgency:** {urgency}{f' | ICU candidate: {icu}' if icu else ''}
- **Key findings:** {findings_str}
"""
        except Exception as e:
            print(f"Failed to parse current_profile: {e}")

    # ── Build historical twin context block ───────────────────────────────────
    twin_ctx = f"""
## Historical Twin Case
{case_text[:800]}
"""

    # ── System prompt ─────────────────────────────────────────────────────────
    system_prompt = (
        "You are an expert clinical reasoning assistant. "
        "Consult the two medical cases below and answer the clinician's question. "
        "Keep your answer EXTREMELY short (maximum 3 sentences or 3 bullet points total). "
        "Use Markdown formatting (bullet points, **bold** text). "
        "CRITICAL INSTRUCTIONS: Do NOT generate long repetitive lists. Never use more than 3 bullet points. "
        "Do NOT add introductory filler. Jump straight into the clinical facts.\n"
        "IMPORTANT: Do NOT append a 'Final Answer:' section or use mathematical LaTeX boxes (\\boxed{}). Just provide the direct text response.\n\n"
        f"{twin_ctx}"
        f"{current_ctx}"
        "\n---\n"
        f"Question: {query}\n\n"
        "Expert Answer:"
    )

    dummy_img = Image.new("RGB", (336, 336), color=(0, 0, 0))
    try:
        import asyncio
        stop_words = ["Final Answer:", "Final Answer", "---\nQuestion:", "Question:"]
        resp = await asyncio.to_thread(query_medgemma, dummy_img, prompt=system_prompt, max_tokens=350, stop_sequences=stop_words)
        if isinstance(resp, list) and len(resp) > 0:
            reply = resp[0].get("generated_text", "").strip()
            
            # Cleanly strip prompt echoing without relying on arbitrary [-50:] slices:
            if "Expert Answer:" in reply:
                reply = reply.split("Expert Answer:")[-1].strip()
            elif f"Question: {query}" in reply:
                reply = reply.split(f"Question: {query}")[-1].strip()

            # Strip mathematical "Final Answer:" boxed formatting AND loops
            import re
            
            # The ultimate loop killer: If it generated "Final Answer" at all, 
            # throw away everything from that point onward forever.
            if "Final Answer" in reply:
                reply = reply.split("Final Answer")[0].strip()
                
            reply = re.sub(r"\\boxed{", "", reply)
            
            # Remove trailing closing brace from LaTeX box if it exists at the end
            if reply.endswith("}"):
                reply = reply[:-1].strip()
                
            # Clean up leading non-word artifacts if model started weirdly
            reply = re.sub(r"^[\W_]+", "", reply)

            # BRUTAL DEDUPLICATION: Kill repeating lines (AI stuttering)
            lines = [line.strip() for line in reply.split('\n') if line.strip()]
            seen = set()
            dedupped_lines = []
            for line in lines:
                # Use a slightly normalized version of the line for matching to catch slight variations
                norm_line = re.sub(r'\W+', '', line.lower())
                if norm_line not in seen:
                    seen.add(norm_line)
                    dedupped_lines.append(line)
            
            # Rejoin the cleaned lines
            reply = '\n\n'.join(dedupped_lines)

            if not reply:
                reply = "I don't have enough information in the provided case context to answer that."
            return {"reply": reply}
    except Exception as e:
        print(f"MedGemma chat error: {e}")

    return {"reply": "I'm sorry, I couldn't reach the AI reasoning engine to answer this question right now."}



# ──────────────────────────────────────────────────────────────────────────────
# /enhance_profile  – MedGemma generates deep Clinical Synthesis
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/enhance_profile")
async def enhance_profile(
    profile_json: str = Form(...),
    file: Optional[UploadFile] = File(None)
):
    """
    Takes the structured case profile JSON and an optional image,
    and asks MedGemma to synthesize hidden insights, missing risk factors,
    and prognostic observations into a 3-4 sentence Markdown block.
    """
    try:
        profile_data = json.loads(profile_json)
        
        # Build context from profile
        age = profile_data.get("patient", {}).get("age_years", "")
        sex = profile_data.get("patient", {}).get("sex", "")
        cc = profile_data.get("presentation", {}).get("chief_complaint", "")
        hpi = profile_data.get("presentation", {}).get("hpi", "")
        pmh = profile_data.get("presentation", {}).get("pmh", "")
        dx = profile_data.get("assessment", {}).get("diagnosis_primary", "")
        comorb = ", ".join(profile_data.get("patient", {}).get("comorbidities", []) or [])
        
        ctx = f"Patient: {age}y {sex}\nCC: {cc}\nHPI: {hpi}\nPMH: {pmh}\nComorbidities: {comorb}\nPrimary Dx: {dx}"
        
        system_prompt = (
            "You are an expert clinical reasoning assistant. "
            "Review the patient profile below (and the image if provided). "
            "Write an 'AI Clinical Synthesis' providing deep medical insights, potential "
            "hidden risk factors, or prognostic observations that are NOT just repeating the provided text. "
            "Keep your synthesis to EXACTLY 3-4 short sentences or bullet points. "
            "Use Markdown format (bold key terms). Do NOT generate repetitive lists. "
            "Do NOT append 'Final Answer:'. Do not include intro filler.\n\n"
            f"## Case Profile\n{ctx[:800]}\n\n"
            "Clinical Synthesis:"
        )

        img = None
        has_image = False
        if file and file.filename:
            content = await file.read()
            img = Image.open(io.BytesIO(content)).convert("RGB")
            has_image = True
        else:
            img = Image.new("RGB", (336, 336), color=(0, 0, 0))

        import asyncio
        stop_words_synthesis = ["Final Answer:", "Final Answer", "---\nClinical Synthesis:", "Clinical Synthesis:"]
        
        # Prepare concurrent tasks
        tasks = [
            asyncio.to_thread(query_medgemma, img, prompt=system_prompt, max_tokens=250, stop_sequences=stop_words_synthesis)
        ]
        
        # If image exists, add a second task for Imaging Context
        if has_image:
            imaging_prompt = (
                "You are an expert radiologist. "
                "Review the provided medical image and the patient's brief clinical context below. "
                "Write an 'Imaging Context' summary focusing strictly on the key radiological findings, "
                "their severity, and their direct clinical relevance to the patient's presentation. "
                "Keep it to EXACTLY 2-3 short sentences. "
                "Use Markdown format (bold key terms). Do NOT generate repetitive lists. "
                "Do NOT append 'Final Answer:'.\n\n"
                f"## Case Context\n{ctx[:500]}\n\n"
                "Imaging Context:"
            )
            stop_words_imaging = ["Final Answer:", "Final Answer", "---\nImaging Context:", "Imaging Context:"]
            tasks.append(
                asyncio.to_thread(query_medgemma, img, prompt=imaging_prompt, max_tokens=200, stop_sequences=stop_words_imaging)
            )

        # Execute concurrently
        results = await asyncio.gather(*tasks)
        
        # --- Process Synthesis ---
        resp_synthesis = results[0]
        reply_synthesis = ""
        if isinstance(resp_synthesis, list) and len(resp_synthesis) > 0:
            reply_synthesis = resp_synthesis[0].get("generated_text", "").strip()
            
            if "Clinical Synthesis:" in reply_synthesis:
                reply_synthesis = reply_synthesis.split("Clinical Synthesis:")[-1].strip()
            if "Final Answer" in reply_synthesis:
                reply_synthesis = reply_synthesis.split("Final Answer")[0].strip()
                
            import re
            reply_synthesis = re.sub(r"\\boxed{", "", reply_synthesis)
            if reply_synthesis.endswith("}"): reply_synthesis = reply_synthesis[:-1].strip()
            reply_synthesis = re.sub(r"^[\W_]+", "", reply_synthesis)

            # Deduplication
            lines = [line.strip() for line in reply_synthesis.split('\n') if line.strip()]
            seen = set()
            dedupped_lines = []
            for line in lines:
                norm_line = re.sub(r'\W+', '', line.lower())
                if norm_line not in seen:
                    seen.add(norm_line)
                    dedupped_lines.append(line)
            reply_synthesis = '\n\n'.join(dedupped_lines)

        if not reply_synthesis:
            reply_synthesis = "Unable to generate clinical synthesis."

        # --- Process Imaging Context (if available) ---
        reply_imaging = None
        if has_image and len(results) > 1:
            resp_imaging = results[1]
            if isinstance(resp_imaging, list) and len(resp_imaging) > 0:
                reply_imaging = resp_imaging[0].get("generated_text", "").strip()
                
                if "Imaging Context:" in reply_imaging:
                    reply_imaging = reply_imaging.split("Imaging Context:")[-1].strip()
                if "Final Answer" in reply_imaging:
                    reply_imaging = reply_imaging.split("Final Answer")[0].strip()
                    
                import re
                reply_imaging = re.sub(r"\\boxed{", "", reply_imaging)
                if reply_imaging.endswith("}"): reply_imaging = reply_imaging[:-1].strip()
                reply_imaging = re.sub(r"^[\W_]+", "", reply_imaging)

                # Deduplication
                lines = [line.strip() for line in reply_imaging.split('\n') if line.strip()]
                seen = set()
                dedupped_lines = []
                for line in lines:
                    norm_line = re.sub(r'\W+', '', line.lower())
                    if norm_line not in seen:
                        seen.add(norm_line)
                        dedupped_lines.append(line)
                reply_imaging = '\n\n'.join(dedupped_lines)

        return {
            "synthesis": reply_synthesis,
            "imaging_context": reply_imaging
        }
            
    except Exception as e:
        print(f"MedGemma enhance error: {e}")
        return {"synthesis": "I'm sorry, I couldn't generate the clinical synthesis right now.", "imaging_context": None}

    return {"synthesis": "Unable to process the request.", "imaging_context": None}


# ──────────────────────────────────────────────────────────────────────────────
# /explain_selection  – MedGemma explains a highlighted phrase in context
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/explain_selection")
async def explain_selection(
    selected_text: str = Form(...),
    context: str = Form(default=""),
):
    """
    Given a short highlighted phrase and its surrounding context,
    ask MedGemma to explain it in 1-2 plain-language clinical sentences.
    """
    context_snippet = context[:500].strip()
    prompt = (
        f"You are a concise medical education assistant. "
        f"Explain the following medical term or phrase in exactly 1-2 sentences, "
        f"suitable for a clinical audience. "
        f"Phrase: \"{selected_text}\". "
        f"Context: \"{context_snippet}\". "
        f"Do NOT repeat the phrase back as a complete sentence. Start directly with the explanation."
    )

    dummy_img = Image.new("RGB", (336, 336), color=(0, 0, 0))
    try:
        import asyncio
        resp = await asyncio.to_thread(query_medgemma, dummy_img, prompt=prompt, max_tokens=120)
        explanation = ""
        if isinstance(resp, list) and len(resp) > 0:
            raw = resp[0].get("generated_text", "").strip()
            # Strip echoed prompt if model returns it
            for marker in ["Start directly with the explanation.", context_snippet, selected_text]:
                if marker and raw.endswith(marker) is False and marker in raw:
                    raw = raw.split(marker)[-1].strip()
            # Keep only first 2 sentences
            import re as _re
            sentences = _re.split(r"(?<=[.!?])\s+", raw)
            explanation = " ".join(sentences[:2]).strip()

        if not explanation:
            explanation = f'"{selected_text}" — a medical term relevant to this clinical case.'

        return {"explanation": explanation}
    except Exception as e:
        print(f"MedGemma explain_selection error: {e}")
        return {"explanation": f'"{selected_text}" — unable to reach the AI explanation engine right now.'}


# ──────────────────────────────────────────────────────────────────────────────
# /extract  – mock CaseProfile extraction
# When MedGemma becomes available, replace _extract_profile() body only.
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/extract")
async def extract(
    images: Optional[List[UploadFile]] = File(default=None),
    notes: str = Form(default=""),
    notes_file: Optional[UploadFile] = File(default=None),
):
    """
    Extract a structured CaseProfile from uploaded images and/or clinical notes.
    Currently uses regex-based mock extraction; replace with MedGemma when ready.
    """
    image_names: list[str] = []
    if images:
        for img in images:
            if img.filename:
                image_names.append(img.filename)

    # Extract document text by file format. In particular, never decode PDF
    # bytes as UTF-8, which would leak `%PDF-...` syntax into the clinical HPI.
    notes_text = notes
    if notes_file:
        try:
            raw = await notes_file.read()
            document_text = extract_document_text(notes_file.filename, notes_file.content_type, raw)
            notes_text = (notes_text + "\n" + document_text).strip()
        except DocumentExtractionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    profile = await _extract_profile(images, notes_text)
    return {"profile": profile}


async def _extract_profile(images: Optional[List[UploadFile]], text: str) -> dict:
    """
    Extracts a structured CaseProfile. 
    If images are provided, it uses MedGemma to analyze them alongside the text notes.
    Otherwise, it falls back to the regex-based mock.
    """
    image_names = [img.filename for img in images if img.filename] if images else []
    
    # If we have an image, let's try to get real insights from MedGemma
    medgemma_insight = ""
    if images and len(images) > 0:
        try:
            # Read the first image for analysis
            contents = await images[0].read()
            # Reset seek position if it's going to be used elsewhere, though here we are done with it
            images[0].file.seek(0) 
            img_pil = Image.open(io.BytesIO(contents)).convert("RGB")
            
            prompt = f"Analyze this chest X-ray image in the context of these clinical notes: '{text}'. Identify key findings like consolidation, effusion, or cardiomegaly. Be structured."
            response = query_medgemma(img_pil, prompt=prompt, max_tokens=300)
            
            if isinstance(response, list) and len(response) > 0:
                medgemma_insight = response[0].get("generated_text", "")
        except Exception as e:
            print(f"MedGemma extraction error: {e}")

    case_id = str(uuid.uuid4())
    image_id = str(uuid.uuid4())

    profile: dict = {
        "profile_id": f"{case_id}:{image_id}",
        "case_id": case_id,
        "image_id": image_id,
        "patient": {
            "age_years": None,
            "sex": None,
            "immunocompromised": None,
            "weight_kg": None,
            "comorbidities": [],
            "medications": [],
            "allergies": None,
        },
        "presentation": {
            "chief_complaint": None,
            "symptom_duration": None,
            "hpi": None,
            "pmh": None,
        },
        "study": {
            "modality": None,
            "body_region": None,
            "view_position": None,
            "radiology_region": None,
            "caption": None,
            "image_type": None,
            "image_subtype": None,
            "image_url": None,
            "storage_path": None,
        },
        "assessment": {
            "diagnosis_primary": None,
            "suspected_primary": [],
            "differential": [],
            "urgency": None,
            "infectious_concern": None,
            "icu_candidate": None,
        },
        "findings": {
            "lungs": {
                "consolidation_present": "no",
                "consolidation_locations": [],
                "consolidation_extent": "unknown",
                "atelectasis_present": "no",
                "atelectasis_locations": [],
                "edema_present": "no",
                "edema_pattern": "unknown",
            },
            "pleura": {
                "effusion_present": "no",
                "effusion_side": "unknown",
                "effusion_size": "unknown",
                "pneumothorax_present": "no",
                "pneumothorax_side": "unknown",
            },
            "cardiomediastinal": {
                "cardiomegaly": "no",
                "mediastinal_widening": "no",
            },
            "devices": {
                "lines_tubes_present": "no",
                "device_list": [],
            },
        },
        "summary": {
            "one_liner": None,
            "key_points": [],
            "red_flags": [],
        },
        "outcome": {
            "success": None,
            "detail": None,
        },
        "provenance": {
            "dataset_name": None,
            "pmc_id": None,
            "pmid": None,
            "doi": None,
            "article_title": None,
            "journal": None,
            "year": None,
            "authors": [],
            "license": None,
            "source_url": None,
        },
        "tags": {
            "ml_labels": [],
            "gt_labels": [],
            "keywords": [],
            "mesh_terms": [],
        },
    }

    # ── Patient ──────────────────────────────────────────────────────────────
    age_m = re.search(r"(\d{1,3})\s*[- ]?(?:year|yr)s?[- ]?old", text, re.I)
    if age_m:
        profile["patient"]["age_years"] = int(age_m.group(1))

    if re.search(r"\bfemale\b|\bwoman\b", text, re.I):
        profile["patient"]["sex"] = "female"
    elif re.search(r"\bmale\b|\bman\b", text, re.I):
        profile["patient"]["sex"] = "male"

    if re.search(r"immunocompromised|immunosuppressed", text, re.I):
        profile["patient"]["immunocompromised"] = "yes"
    elif text.strip():
        profile["patient"]["immunocompromised"] = "no"

    comorbidity_map = [
        (r"hypertension|HTN", "hypertension"),
        (r"type 2 diabet|T2DM|DM2", "type 2 diabetes"),
        (r"type 1 diabet|T1DM|DM1", "type 1 diabetes"),
        (r"atrial fibrillation|AF\b|AFib", "atrial fibrillation"),
        (r"heart failure|CHF", "heart failure"),
        (r"COPD|chronic obstructive", "COPD"),
        (r"asthma", "asthma"),
        (r"cirrhosis|liver cirrhosis", "liver cirrhosis"),
        (r"hepatocellular carcinoma|HCC", "hepatocellular carcinoma"),
        (r"chronic kidney|CKD", "chronic kidney disease"),
        (r"coronary artery disease|CAD", "coronary artery disease"),
        (r"obesity", "obesity"),
    ]
    comorbidities = [label for pattern, label in comorbidity_map if re.search(pattern, text, re.I)]
    profile["patient"]["comorbidities"] = comorbidities

    if re.search(r"no known allerg", text, re.I):
        profile["patient"]["allergies"] = "no known allergies"

    # ── Presentation ─────────────────────────────────────────────────────────
    cc_m = re.search(
        r"(?:present(?:ing)? with|complaint of|admitted for|scheduled for)\s+([^.!?\n]{5,120})",
        text, re.I
    )
    if cc_m:
        profile["presentation"]["chief_complaint"] = cc_m.group(1).strip()

    dur_m = re.search(r"(?:for|over|duration of)\s+((?:\d+\s*)?(?:day|week|month|year)s?)", text, re.I)
    if dur_m:
        profile["presentation"]["symptom_duration"] = dur_m.group(1).strip()

    if len(text) > 40:
        profile["presentation"]["hpi"] = text[:600]

    if comorbidities:
        profile["presentation"]["pmh"] = ", ".join(comorbidities)

    # ── Study ────────────────────────────────────────────────────────────────
    combined = text + " " + " ".join(image_names)
    if re.search(r"ct|computed tomography", combined, re.I):
        profile["study"].update({"modality": "CT", "image_type": "radiology", "image_subtype": "ct"})
    elif re.search(r"mri", combined, re.I):
        profile["study"].update({"modality": "MRI", "image_type": "radiology", "image_subtype": "mri"})
    elif re.search(r"x[- ]?ray|cxr|chest x", combined, re.I):
        profile["study"].update({"modality": "CXR", "image_type": "radiology", "image_subtype": "x_ray"})
    elif image_names:
        profile["study"].update({"modality": "Imaging", "image_type": "radiology"})

    if re.search(r"thorax|chest|pulmonary|lung", text, re.I):
        profile["study"]["body_region"] = "thorax"
        profile["study"]["radiology_region"] = "thorax"
    elif re.search(r"abdomen|abdominal|liver", text, re.I):
        profile["study"]["body_region"] = "abdomen"
    elif re.search(r"brain|head|neuro", text, re.I):
        profile["study"]["body_region"] = "head"

    if re.search(r"\bPA\b|posteroanterior", text, re.I):
        profile["study"]["view_position"] = "PA"
    elif re.search(r"\bAP\b|anteroposterior", text, re.I):
        profile["study"]["view_position"] = "AP"

    # ── Assessment ───────────────────────────────────────────────────────────
    diag_map = [
        (r"scimitar", "scimitar syndrome"),
        (r"pneumonia", "community-acquired pneumonia"),
        (r"pulmonary embolism|PE\b", "pulmonary embolism"),
        (r"lung malignancy|lung cancer|NSCLC|SCLC", "lung malignancy"),
        (r"stroke|ischemic", "acute ischemic stroke"),
        (r"heart failure|pulmonary edema", "heart failure"),
        (r"pneumothorax", "pneumothorax"),
        (r"pleural effusion", "pleural effusion"),
        (r"aortic dissection", "aortic dissection"),
    ]
    for pattern, diag in diag_map:
        if re.search(pattern, text, re.I):
            profile["assessment"]["diagnosis_primary"] = diag
            profile["assessment"]["suspected_primary"] = [diag] + comorbidities[:2]
            break

    if re.search(r"urgent|emergency|stat", text, re.I):
        profile["assessment"]["urgency"] = "emergent"
    elif re.search(r"routine|elective|scheduled", text, re.I):
        profile["assessment"]["urgency"] = "routine"
    elif text.strip():
        profile["assessment"]["urgency"] = "semi-urgent"

    profile["assessment"]["infectious_concern"] = (
        "yes" if re.search(r"infection|sepsis|pneumonia|fever", text, re.I) else "no"
    )
    profile["assessment"]["icu_candidate"] = (
        "yes" if re.search(r"icu|intensive care|critical", text, re.I) else "no"
    )

    # ── Findings tweaks ──────────────────────────────────────────────────────
    # ── MedGemma Insight Integration ──────────────────────────────────────────
    # We combine the original text with MedGemma's findings for the regex extractor
    # to pick up confirmed findings from the image.
    analysis_text = text + "\n" + medgemma_insight

    if re.search(r"consolidation|consolidat", analysis_text, re.I):
        profile["findings"]["lungs"]["consolidation_present"] = "yes"
    if re.search(r"atelectasis|collapse", analysis_text, re.I):
        profile["findings"]["lungs"]["atelectasis_present"] = "yes"
    if re.search(r"edema|pulmonary edema", analysis_text, re.I):
        profile["findings"]["lungs"]["edema_present"] = "yes"
    if re.search(r"effusion|pleural fluid", analysis_text, re.I):
        profile["findings"]["pleura"]["effusion_present"] = "yes"
    if re.search(r"pneumothorax", analysis_text, re.I):
        profile["findings"]["pleura"]["pneumothorax_present"] = "yes"
    if re.search(r"cardiomegaly|enlarged heart|cardiomegal", analysis_text, re.I):
        profile["findings"]["cardiomediastinal"]["cardiomegaly"] = "yes"

    if medgemma_insight and not profile["summary"]["one_liner"]:
        profile["summary"]["one_liner"] = medgemma_insight[:200] + ("..." if len(medgemma_insight) > 200 else "")

    # ── Summary ──────────────────────────────────────────────────────────────
    age = profile["patient"]["age_years"]
    sex = profile["patient"]["sex"]
    diag = profile["assessment"]["diagnosis_primary"]
    cc   = profile["presentation"]["chief_complaint"]
    if age and sex and (diag or cc):
        comorbs = ", ".join(comorbidities[:3]) or "multiple comorbidities"
        profile["summary"]["one_liner"] = (
            f"{age}-year-old {sex} with {comorbs} presenting with {cc or diag}."
        )
    if diag:
        profile["summary"]["key_points"] = [f"Primary finding: {diag}"]

    # ── Extra Fields (schema expansion) ──────────────────────────────────────
    # Scan for clinical data that doesn't fit the base schema.
    # These are captured at ANY point during intake (any confidence level).
    extra_fields: dict = {}

    # Smoking / tobacco
    smoke_m = re.search(
        r"(?:smok(?:ing|er|es)|tobacco)[^\.\n]{0,60}?((?:\d+\s*)?(?:pack[- ]?year|cigarette|cigar|pipe)[^\.\n]{0,40})?",
        text, re.I
    )
    if smoke_m:
        detail = smoke_m.group(1)
        extra_fields["smoking_status"] = detail.strip() if detail and detail.strip() else "smoker"

    # Never smoked
    if re.search(r"non[- ]?smok|never smoked|no smoking", text, re.I):
        extra_fields["smoking_status"] = "non-smoker"

    # Alcohol use
    alcohol_m = re.search(r"alcohol[^\.\n]{0,80}", text, re.I)
    if alcohol_m:
        snippet = alcohol_m.group(0).strip()
        extra_fields["alcohol_use"] = snippet[:120]

    # BMI / weight / height
    bmi_m = re.search(r"BMI\s*(?:of\s*)?(\d{1,2}(?:\.\d)?)", text, re.I)
    if bmi_m:
        extra_fields["bmi"] = bmi_m.group(1)

    height_m = re.search(r"(\d{1,3})\s*(?:cm|ft|feet|inches?)", text, re.I)
    if height_m and "bmi" not in extra_fields:
        extra_fields["height"] = f"{height_m.group(1)} {height_m.group(0).split(height_m.group(1))[-1].strip()}"

    # Blood type
    blood_m = re.search(r"\b(A|B|AB|O)[+-]?\s*blood\s*type|\bblood\s*type\s*(A|B|AB|O)[+-]?\b", text, re.I)
    if blood_m:
        extra_fields["blood_type"] = (blood_m.group(1) or blood_m.group(2)).upper()

    # Family history
    fam_m = re.search(r"family\s*(?:history|hx)[^\.\n]{0,150}", text, re.I)
    if fam_m:
        extra_fields["family_history"] = fam_m.group(0).strip()[:200]

    # Occupation / employment
    occ_m = re.search(r"(?:occupation|works?\s*as|employed\s*(?:as|at)|profession)[^\.\n]{0,80}", text, re.I)
    if occ_m:
        extra_fields["occupation"] = occ_m.group(0).strip()[:120]

    # Ethnicity / race
    eth_m = re.search(
        r"(?:ethnicity|race|racial background)\s*[:\-]?\s*([A-Za-z\s\-]+)",
        text, re.I
    )
    if eth_m:
        extra_fields["ethnicity"] = eth_m.group(1).strip()[:60]

    # Vaccination status
    vax_m = re.search(r"(?:vaccin|immuniz)[^\.\n]{0,80}", text, re.I)
    if vax_m:
        extra_fields["vaccination"] = vax_m.group(0).strip()[:120]

    # Travel history
    travel_m = re.search(r"(?:travel(?:led|ed)?\s*(?:to|from)|recent\s*travel)[^\.\n]{0,100}", text, re.I)
    if travel_m:
        extra_fields["travel_history"] = travel_m.group(0).strip()[:150]

    # Functional status / ADLs
    func_m = re.search(r"(?:functional status|ADLs?|activities of daily|ambulates?|independent)[^\.\n]{0,80}", text, re.I)
    if func_m:
        extra_fields["functional_status"] = func_m.group(0).strip()[:120]

    # Code status / DNR
    code_m = re.search(r"(?:code\s*status|full\s*code|DNR|DNI|comfort\s*care)[^\.\n]{0,60}", text, re.I)
    if code_m:
        extra_fields["code_status"] = code_m.group(0).strip()[:80]

    # Social history (catch-all if not already captured)
    social_m = re.search(r"social\s*(?:history|hx)[^\.\n]{0,200}", text, re.I)
    if social_m:
        extra_fields["social_history"] = social_m.group(0).strip()[:250]

    if extra_fields:
        profile["extra_fields"] = extra_fields
    else:
        profile["extra_fields"] = {}

    return profile

# ──────────────────────────────────────────────────────────────────────────────
# /analyze_hospital_page  – CrewAI Agent endpoint for doctor extraction
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/analyze_hospital_page")
async def analyze_hospital_page(
    url: str = Form(...),
    diagnosis: str = Form(...),
    hospital_name: str = Form(default=""),
    location: str = Form(default="")
):
    """
    Triggers the CrewAI agent to scrape the given hospital URL and return doctors
    specializing in the given diagnosis.
    """
    import asyncio
    import agents
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [analyze_hospital_page] Received request for {hospital_name} (location: {location})")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [analyze_hospital_page] LANGCHAIN_TRACING_V2={os.getenv('LANGCHAIN_TRACING_V2')}")
    
    try:
        # Run CrewAI synchronously inside an async thread to prevent blocking Uvicorn
        data = await asyncio.to_thread(agents.analyze_hospital_staff, url, diagnosis, hospital_name, location)
        return {"specialists": data}
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Agent endpoint failed: {e}")
        return {"specialists": [], "error": str(e)}
