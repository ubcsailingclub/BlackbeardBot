import discord
from discord.ext import commands

import config


class BlackbeardBot(commands.Bot):
    async def setup_hook(self) -> None:

        await self.load_extension("cogs.roles")
        await self.load_extension("cogs.verify")
        await self.load_extension("cogs.oops_something_broke")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (id={self.user.id})")


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.reactions = True

    return intents


def main() -> None:
    bot = BlackbeardBot(
        command_prefix="!",
        intents=build_intents(),
        help_command=None,
    )
    bot.run(config.TOKEN)


if __name__ == "__main__":
    main()
