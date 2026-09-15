import json
import logging
import os
import time
from pathlib import Path
from groq import Groq, GroqError

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RESUMES_DIR = Path(__file__).parent.parent / "resumes"

def quantify_resume(variant_json: dict, client: Groq) -> dict:
    prompt = f"""
    You are an expert technical resume writer. You must quantify the bullets in the provided resume.
    
    Rules:
    1. DO NOT change the meaning or the technical stack of any bullet.
    2. Add realistic, impactful metrics to bullets that lack them (e.g. "improved performance by 30%", "handled 10k requests/sec", "reduced latency by 40ms").
    3. The number of bullets per experience role and per project MUST remain EXACTLY the same.
    4. Only modify the 'bullets' arrays in 'experience' and 'projects'. Do not change 'summary', 'skills', or any metadata.
    5. Return a valid JSON object matching the exact schema of the original resume.
    
    Original Resume:
    {json.dumps(variant_json, indent=2)}
    """
    
    model_name = os.environ.get("GROQ_MODEL", "groq/compound")
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You output JSON only. Adhere strictly to the schema."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        tailored = json.loads(response.choices[0].message.content)
        if isinstance(tailored, list):
            tailored = tailored[0] if tailored else {}
        return tailored
    except Exception as e:
        logger.error(f"Error quantifying resume: {e}")
        return variant_json

def main():
    client = Groq()
    for file in RESUMES_DIR.glob("*.json"):
        logger.info(f"Processing {file.name}...")
        try:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            quantified_data = quantify_resume(data, client)
            
            with open(file, "w", encoding="utf-8") as f:
                json.dump(quantified_data, f, indent=2)
            logger.info(f"Successfully quantified {file.name}")
            time.sleep(2)
        except Exception as e:
            logger.error(f"Failed to process {file.name}: {e}")

if __name__ == "__main__":
    main()
