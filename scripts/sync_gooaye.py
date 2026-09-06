import asyncio

from app.db import init_db
from app.services.sync import sync_gooaye


async def main() -> None:
    init_db()
    result = await sync_gooaye()
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
