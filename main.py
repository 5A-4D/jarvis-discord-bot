import asyncio
import logging

from jarvis_bot.bot import run_bot


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The receive extension reports routine RTCP sender reports as unexpected at
    # INFO level. They are harmless and otherwise produce one log line per second.
    logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)
    logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.WARNING)
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Jarvis stopped by operator")
