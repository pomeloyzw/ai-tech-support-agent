import asyncio
import sys
import logging
from agent.orchestrator import process_case

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

async def main():
    case_id = "c5682b56-9548-4728-b276-d00bf36e8675"
    print(f"Retrying case {case_id}...")
    await process_case(case_id)
    print("Done!")

if __name__ == "__main__":
    asyncio.run(main())
