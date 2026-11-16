"""`/mirror`: download a link and store the files on the bot's FTP server.

The download half is `/leech` verbatim -- same engines, same options, same
keyboard -- because the only difference is where the finished files go. The
destination is forced with ``forced_destination`` rather than offered as a flag:
this bot reaches telegram with `/leech` and a bucket with `-s3`, so `/mirror` is
the command whose job is FTP. A keyboard toggle that silently did nothing would
be worse than no toggle, which is why the destination flags are not offered to
it either (``option_command``).
"""

from .. import bot_loop
from ..helper.telegram.bot_commands import BotCommands
from .leech import Leech


class Mirror(Leech):
    handler = "mirror"
    command = BotCommands.MirrorCommand[0]
    option_command = "mirror"
    forced_destination = "ftp"


async def mirror(client, message):
    bot_loop.create_task(Mirror(client, message).new_event())
