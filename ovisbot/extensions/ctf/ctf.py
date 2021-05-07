import copy
import discord
import datetime
import logging
import sys
import re
import requests
import dateutil.parser
import pytz
import random

import ovisbot.locale as i18n

from datetime import timedelta
from dateutil.utils import default_tzinfo
from discord.ext import commands
from pymodm.errors import ValidationError
from ovisbot.db_models import CTF, Challenge
from ovisbot.exceptions import (
    CTFAlreadyExistsException,
    FewParametersException,
    ChallengeAlreadySolvedException,
    ChallengeInvalidCategory,
    ChallengeInvalidDifficulty,
    ChallengeExistsException,
    ChallengeDoesNotExistException,
    NotInChallengeChannelException,
    CTFAlreadyFinishedException,
    ChallengeNotSolvedException,
    UserAlreadyInChallengeChannelException,
    CTFSharedCredentialsNotSet,
    CtfimeNameDoesNotMatch,
    DateMisconfiguredException,
    MissingStartDateException,
    MissingEndDateException,
)
from ovisbot.helpers import (
    chunkify,
    create_corimd_notebook,
    escape_md,
    failed,
    success,
    td_format,
)
from discord.ext import tasks
from ovisbot.locale import tz
from discord.ext.commands.core import GroupMixin
import pymodm

logger = logging.getLogger(__name__)

CHALLENGE_CATEGORIES = [
    "crypto",
    "web",
    "misc",
    "pwn",
    "reverse",
    "stego",
    "forensics",
    "osint",
    "htb",
    "hardware",
]

CHALLENGE_DIFFICULTIES = [
    "none",
    "easy",
    "medium",
    "hard"
]

# difficulty -> (:emoji:, text)
DIFFICULTY_REWARDS = {
    "none": (":candy:", "κουφεττούα"),
    "easy": (":cookie:", "πισκοττούιν"),
    "medium": (":lollipop:", ["μηλούιν", "πίππιλλο", "γλειφιτζούρι"]),
    "hard": (":icecream:", "παωτόν"),
}

class Ctf(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_reminders.start()

    def _get_channel_ctf(self, ctx):
        channel_name = str(ctx.channel.category)
        return CTF.objects.get({"name": channel_name})

    @commands.group()
    async def ctf(self, ctx):
        """
        Collection of commands to manage CTFs
        """
        self.guild = ctx.guild
        self.gid = ctx.guild.id

        if ctx.invoked_subcommand is None:
            await ctx.send("Invalid command passed.  Use !help.")

    @ctf.command()
    async def status(self, ctx):
        """
        Returns a list of ongoing challenges in the ctf
        """
        channel_name = str(ctx.channel.category)
        ctf = CTF.objects.get({"name": channel_name})
        summary_chunks = chunkify(ctf.challenge_summary(), 1700)
        for chunk in summary_chunks:
            emb = discord.Embed(description=chunk, colour=4387968)
            await ctx.channel.send(embed=emb)

    @status.error
    async def status_error(self, ctx, error):
        if isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "Έφκαλεν η γλώσσα μου μαλλιά ρε! For this command you have to be in a channel created by !ctf create."
            )

    @ctf.command()
    async def archive(self, ctx, ctf_name):
        """
        Arcive a CTF to the DB and remove it from discord
        """
        ctf_name = ctf_name.lower()
        ctf = CTF.objects.get({"name": ctf_name})
        category = discord.utils.get(ctx.guild.categories, name=ctf_name)
        ctfrole = discord.utils.get(ctx.guild.roles, name="Team-" + ctf_name)

        if ctfrole is not None:
            await ctfrole.delete()
        for c in category.channels:
            await c.delete()

        await category.delete()
        ctf.name = "__ARCHIVED__" + ctf.name  # bug fix (==)
        ctf.save()

    @archive.error
    async def archive_error(self, ctx, error):
        if isinstance(error, commands.errors.MissingRequiredArgument):
            self.bot.help_command.context = ctx
            await self.bot.help_command.command_callback(ctx, command=str(ctx.command))

        if isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "Πε μου εσύ αν θορείς κανένα CTF με έτσι όνομα ... Οι πε μου"
            )
        else:
            logger.error(error)
            await ctx.channel.send("Ουπς. Κάτι επήε λάθος.")

    @ctf.command()
    async def create(self, ctx, ctf_name):
        """
        Creates a new CTF
        """
        scat = ctf_name.lower()
        category = discord.utils.get(ctx.guild.categories, name=scat)

        if category is not None:
            raise CTFAlreadyExistsException

        user = ctx.message.author
        ctfrole = await self.guild.create_role(name="Team-" + scat, mentionable=True)
        await user.add_roles(ctfrole)
        overwrites = {
            self.guild.get_role(self.gid): discord.PermissionOverwrite(
                read_messages=False
            ),
            self.bot.user: discord.PermissionOverwrite(read_messages=True),
            ctfrole: discord.PermissionOverwrite(read_messages=True),
        }
        category = await self.guild.create_category(name=scat, overwrites=overwrites)
        general_channel = await self.guild.create_text_channel(
            name="general", category=category
        )
        CTF(name=category, created_at=datetime.datetime.now(), challenges=[]).save()
        await success(ctx.message)
        await general_channel.send(f"@here Καλως ορίσατε στο {category} CTF")

    @create.error
    async def create_error(self, ctx, error):
        if isinstance(error, commands.errors.MissingRequiredArgument):
            self.bot.help_command.context = ctx
            await self.bot.help_command.command_callback(ctx, command=str(ctx.command))

        if isinstance(error.original, CTFAlreadyExistsException):
            await ctx.channel.send(
                "Ρε κουμπάρε! This CTF name already exists! Pick another one"
            )

    @ctf.command(aliases=["addchall"])
    async def addchallenge(self, ctx, challname, category, difficulty="none"):
        """
        Creates a private channel for a challenge.

        Parameters:
            challenge (str): Name of the challenge
            category (str): Name of challenge's category
            difficulty (str): Can be one of ["none", "easy", "medium", "hard"]. Optional, defaults to "none".
        """
        channel_name = str(ctx.channel.category)
        ctf = CTF.objects.get({"name": channel_name})

        challenge_name = challname.lower()

        category = category.lower()
        if category not in CHALLENGE_CATEGORIES:
            raise ChallengeInvalidCategory

        difficulty = difficulty.lower()
        if difficulty not in CHALLENGE_DIFFICULTIES:
            raise ChallengeInvalidDifficulty

        challenges = [
            c for c in ctf.challenges if c.name == challenge_name + "-" + channel_name
        ]
        if len(challenges) > 0:
            raise ChallengeExistsException

        overwrites = {
            self.guild.get_role(self.gid): discord.PermissionOverwrite(
                read_messages=False
            ),
            self.bot.user: discord.PermissionOverwrite(read_messages=True),
            ctx.message.author: discord.PermissionOverwrite(read_messages=True),
        }
        notebook_url = create_corimd_notebook()
        challenge_channel = await ctx.channel.category.create_text_channel(
            challenge_name + "-" + channel_name, overwrites=overwrites
        )
        new_challenge = Challenge(
            name=challenge_channel.name,
            tags=[category, difficulty],
            created_at=datetime.datetime.now(),
            attempted_by=[ctx.message.author.name],
            notebook_url=notebook_url,
        )
        ctf.challenges.append(new_challenge)
        ctf.save()
        await success(ctx.message)
        await challenge_channel.send("@here Ατε να δούμε δώστου πίεση!")
        notebook_msg = await challenge_channel.send(
            f"Ετο τζαι το δευτερούι σου: {notebook_url}"
        )
        await notebook_msg.pin()

    @addchallenge.error
    async def addchallenge_error(self, ctx, error):
        if isinstance(error, commands.errors.MissingRequiredArgument):
            self.bot.help_command.context = ctx
            await self.bot.help_command.command_callback(ctx, command=str(ctx.command))

        if isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "For this command you have to be in a channel created by !ctf create."
            )
        elif isinstance(error.original, ChallengeInvalidCategory):
            await ctx.channel.send(
                "Not valid challenge category provided. !help for more info"
            )
        elif isinstance(error.original, ChallengeInvalidDifficulty):
            await ctx.channel.send(
                "Not valid challenge difficulty provided. !help for more info"
            )
        elif isinstance(error.original, ChallengeExistsException):
            await ctx.channel.send(
                "Να μου γελάσεις ρε κοπελλούι; This challenge already exists!"
            )

    @ctf.command(aliases=["rmchall"])
    async def rmchallenge(self, ctx, challname):
        """
        Removes the challenge with the given name.
        """
        channel_name = str(ctx.channel.category)
        ctf = CTF.objects.get({"name": channel_name})

        challenge_name = challname.lower()
        chall_name = challenge_name + "-" + channel_name
        challenges = [
            c for c in ctf.challenges if c.name == chall_name
        ]
        if len(challenges) == 0:
            raise ChallengeDoesNotExistException

        challenge_channel = discord.utils.get(
            ctx.channel.category.channels, name=chall_name
        )
        for i, c in enumerate(ctf.challenges):
            if c.name == chall_name:
                del ctf.challenges[i]
                break
        await challenge_channel.delete()
        ctf.save()

    @rmchallenge.error
    async def rmchallenge_error(self, ctx, error):
        if isinstance(error, commands.errors.MissingRequiredArgument):
            self.bot.help_command.context = ctx
            await self.bot.help_command.command_callback(ctx, command=str(ctx.command))

        if isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "For this command you have to be in a channel created by !ctf create."
            )
        elif isinstance(error.original, ChallengeDoesNotExistException):
            await ctx.channel.send(
                "Παρέα μου... εν κουτσιάς... Εν έσιει έτσι challenge!"
            )

    @ctf.command()
    async def notes(self, ctx):
        """
        Shows the notebook url for the particular challenge channel that you are currently in. If this command is run outside of a challenge channel, then ovis gets mad.
        """
        chall_name = ctx.channel.name
        ctf = CTF.objects.get({"name": ctx.channel.category.name})

        # Find challenge in CTF by name
        challenge = next((c for c in ctf.challenges if c.name == chall_name), None)

        if not challenge:
            raise NotInChallengeChannelException

        if challenge.notebook_url != "":
            await ctx.channel.send(f"Notes: {challenge.notebook_url}")
        else:
            await ctx.channel.send("Εν έσσιει έτσι πράμα δαμέ...Τζίλα το...")

    @notes.error
    async def notes_error(self, ctx, error):
        if isinstance(
            error.original, (NotInChallengeChannelException, CTF.DoesNotExist)
        ):
            await ctx.channel.send(
                "Ρε πελλοβρεμένε! For this command you have to be in a ctf challenge channel created by `!ctf addchallenge`."
            )

    @ctf.command()
    @commands.has_permissions(manage_channels=True, manage_roles=True)
    async def finish(self, ctx, ctf_name):
        """
        Marks CTF as finished. (Requires manage channels permissions)
        """
        ctf = CTF.objects.get({"name": ctf_name.lower()})
        if ctf.finished_at is not None:
            raise CTFAlreadyFinishedException
        ctf.finished_at = datetime.datetime.now()
        ctf.save()
        await ctx.channel.send(
            "Ατε.. Μπράβο κοπέλια τζαι κοπέλλες! Να πνάσουμε τζαι εμείς νακκο!"
        )

    @finish.error
    async def finish_error(self, ctx, error):
        if isinstance(error, commands.errors.MissingRequiredArgument):
            self.bot.help_command.context = ctx
            await self.bot.help_command.command_callback(ctx, command=str(ctx.command))

        if isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "Εισαι τζαι εσού χαλασμένος όπως τα διαστημόπλοια του Κίτσιου... There is not such CTF name. Use `!status`"
            )
        if isinstance(error.original, CTFAlreadyFinishedException):
            await ctx.channel.send("This CTF has already finished!")

    @ctf.command()
    async def solve(self, ctx):
        """
        Marks the current challenge as solved by you. 
        Addition of team mates that helped to solve is optional
        """
        chall_name = ctx.channel.name
        ctf = CTF.objects.get({"name": ctx.channel.category.name})

        # Find challenge in CTF by name
        challenge = next((c for c in ctf.challenges if c.name == chall_name), None)

        if not challenge:
            raise NotInChallengeChannelException
        if challenge.solved_at:
            raise ChallengeAlreadySolvedException(challenge.solved_by)

        challenge.solved_by = [ctx.message.author.name] + [
            m.name for m in ctx.message.mentions
        ]
        challenge.solved_at = datetime.datetime.now()
        ctf.save()

        solvers_str = escape_md(
            ", ".join(
                [ctx.message.author.name] + [m.name for m in ctx.message.mentions]
            )
        )

        # look for difficulty in challenge's tags
        # default to "none" if no matching difficulty is found
        difficulty = "none"
        tags_lower = [x.lower() for x in challenge.tags]
        for d in DIFFICULTY_REWARDS:
            if d.lower() in tags_lower:
                difficulty = d

        reward = DIFFICULTY_REWARDS[difficulty]
        reward_emoji, reward_text = reward
        if isinstance(reward_text, list):
            reward_text = random.choice(reward_text)

        await ctx.channel.send(
            "Πελλαμός! {0}! Congrats for solving {1}. Έλα {2} {3}".format(
                solvers_str, chall_name, reward_text, reward_emoji
            )
        )

        await ctx.channel.edit(name="\N{CHECK MARK}" + chall_name)
        general_channel = discord.utils.get(
            ctx.channel.category.channels, name="general"
        )
        await general_channel.send(
            f"{solvers_str} solved the {chall_name} challenge! {reward_emoji} {reward_emoji}"
        )

    @solve.error
    async def solve_error(self, ctx, error):
        if isinstance(
            error.original, (NotInChallengeChannelException, CTF.DoesNotExist)
        ):
            await ctx.channel.send(
                "Ρε πελλοβρεμένε! For this command you have to be in a ctf challenge channel created by `!ctf addchallenge`."
            )
        elif isinstance(error.original, ChallengeAlreadySolvedException):
            await ctx.channel.send(
                f'Άρκησες! This challenge has already been solved by {", ".join(error.original.solved_by)}!'
            )

    @ctf.command()
    async def unsolve(self, ctx):
        """
        Marks the current challenge as unsolved. Allows to to rollback accidental solves.
        """
        chall_name = ctx.channel.name
        ctf = CTF.objects.get({"name": ctx.channel.category.name})

        challenge = next((c for c in ctf.challenges if c.name == chall_name), None)

        if not challenge:
            raise NotInChallengeChannelException
        if not challenge.solved_at:
            raise ChallengeNotSolvedException

        challenge.solved_by = None
        challenge.solved_at = None
        ctf.save()

    @unsolve.error
    async def unsolve_error(self, ctx, error):
        if isinstance(
            error.original, (NotInChallengeChannelException, CTF.DoesNotExist)
        ):
            await ctx.channel.send(
                "Ρε πελλοβρεμένε! For this command you have to be in a ctf challenge channel created by `!ctf addchallenge`."
            )
        elif isinstance(error.original, ChallengeNotSolvedException):
            await ctx.channel.send(f"Ρε κουμπάρε.. αφού ένεν λυμένη η ασκηση.")

    @ctf.command()
    async def join(self, ctx, *params):
        """
        Join an ongoing ctf. Use `status` to see available CTFs.
        """
        scat = "-".join(list(params)).replace("'", "").lower()
        CTF.objects.get({"name": scat})
        ctfrole = discord.utils.get(self.guild.roles, name="Team-" + scat)
        await ctx.message.author.add_roles(ctfrole)
        await success(ctx.message)

    @join.error
    async def join_error(self, ctx, error):
        if isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "Εεεε!! Τι κάμνεις? There is no such CTF name. Use `!status`"
            )

    @ctf.command()
    async def leave(self, ctx):
        """
        Leave the current CTF.
        """
        ctf_name = str(ctx.channel.category)
        ctfrole = discord.utils.get(ctx.guild.roles, name="Team-" + ctf_name)
        await ctx.message.author.remove_roles(ctfrole)

    @ctf.command()
    async def attempt(self, ctx, challname):
        """
        Adds you to the private channel of that challenge. Use !ctf status to see active challenges.
        """
        ctf_name = str(ctx.channel.category)
        ctf = CTF.objects.get({"name": ctf_name})

        chall_name = challname.lower()

        if chall_name == "--all":
            for challenge in ctf.challenges:
                if (
                    ctx.message.author.name
                    in challenge.attempted_by
                    # or challenge.solved_at
                ):
                    continue
                challenge.attempted_by = challenge.attempted_by + [
                    ctx.message.author.name
                ]
                chall_channel = discord.utils.get(
                    ctx.channel.category.channels, name=challenge.name
                )
                await chall_channel.set_permissions(
                    ctx.message.author, read_messages=True
                )

            ctf.save()
            await ctx.channel.send(f"Άμα είσαι κουνόσσιηλλος...")
        else:
            challenge = next(
                (c for c in ctf.challenges if c.name == ctf_name + "-" + chall_name),
                None,
            )
            if not challenge:
                raise ChallengeDoesNotExistException
            # if challenge.solved_at:
            #     raise ChallengeAlreadySolvedException
            if ctx.message.author.name in challenge.attempted_by:
                raise UserAlreadyInChallengeChannelException

            chall_channel = discord.utils.get(
                ctx.channel.category.channels, name=ctf_name + "-" + chall_name
            )
            await chall_channel.set_permissions(ctx.message.author, read_messages=True)

            # TODO(investigate): ch.attempted_by.append(ctx.message.author.name) mutates all embedded challenges
            challenge.attempted_by = challenge.attempted_by + [ctx.message.author.name]
            ctf.save()
            await ctx.channel.send(
                f'Άτε {ctx.message.author.name} μου! Έβαλα σε τζιαι στη λίστα τζείνων που μάχουνται πάνω στο challenge. [{", ".join(challenge.attempted_by)}]'
            )

    @attempt.error
    async def attempt_error(self, ctx, error):
        if isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "For this command you have to be in a channel created by !ctf create."
            )
        elif isinstance(error.original, ChallengeDoesNotExistException):
            await ctx.channel.send(
                "Μα περιπέζεις μας; There is not such a challenge. Create it using !ctf addchallenge"
            )
        elif isinstance(error.original, ChallengeAlreadySolvedException):
            await ctx.channel.send("Ρε μπρο μου... Ελύσαμε το τουτο το challenge αφού.")
        elif isinstance(error.original, UserAlreadyInChallengeChannelException):
            await ctx.channel.send("Ρε αρφούι μου... Είσαι ήδη μέσα στη λιστούα.")
        else:
            await ctx.channel.send(
                "Εν τα κατάφερα να σε βάλω μέσα στη λιστούα... Σόρρυ."
            )

    @ctf.command()
    async def description(self, ctx, *, description):
        """
        Sets the description of an existing CTF
        """
        channel_name = str(ctx.channel.category)

        ctf = CTF.objects.get({"name": channel_name})
        ctf.description = " ".join(description)
        ctf.save()
        await success(ctx.message)

    @description.error
    async def description_error(self, ctx, error):
        if isinstance(error.original, FewParametersException):
            await ctx.channel.send(
                "Πεε που σου νέφκω που παεις... !ctf description takes parameters. !help for more info."
            )
        elif isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "For this command you have to be in a channel created by !ctf create."
            )

    @ctf.command()
    @commands.has_permissions(manage_channels=True, manage_roles=True)
    async def delete(self, ctx, ctfname):
        """
        Delete a CTF category with all its channel and its user role.
        """
        scat = ctfname.replace("'", "").lower()
        ctf = CTF.objects.get({"name": scat})
        category = discord.utils.get(ctx.guild.categories, name=scat)
        ctfrole = discord.utils.get(ctx.guild.roles, name="Team-" + scat)

        if ctfrole is not None:
            await ctfrole.delete()

        for c in category.channels:
            await c.delete()
        await category.delete()
        ctf.delete()

    @delete.error
    async def delete_error(self, ctx, error):
        if isinstance(error, commands.errors.MissingRequiredArgument):
            self.bot.help_command.context = ctx
            await self.bot.help_command.command_callback(ctx, command=str(ctx.command))

        if isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "Ρε κουμπάρε! There is not any ongoing CTF with such a name."
            )

    @ctf.command()
    async def setcreds(self, ctx, username, password, link=None):
        """
        Sets shared credentials to be used by the team members to login to the CTF. Link is the login/home page of the CTF
        """
        channel_name = str(ctx.channel.category)
        ctf = CTF.objects.get({"name": channel_name})
        ctf.username = username
        ctf.password = password
        if link:
            logger.info(link)
            ctf.url = link
        ctf.save()
        await success(ctx.message)

        if not (ctf.username and ctf.password):
            raise CTFSharedCredentialsNotSet
        emb = discord.Embed(description=ctf.credentials(), colour=4387968)
        msg = await ctx.channel.send(embed=emb)
        await msg.pin()

    @setcreds.error
    async def setcreds_error(self, ctx, error):
        if isinstance(error, commands.errors.MissingRequiredArgument):
            await ctx.channel.send(
                "Πεε που σου νέφκω που παεις... !ctf setcreds takes 2 or more parameters."
            )
            self.bot.help_command.context = ctx
            await self.bot.help_command.command_callback(ctx, command=str(ctx.command))

        if isinstance(error.original, FewParametersException):
            await ctx.channel.send(
                "Πεε που σου νέφκω που παεις... !ctf setcreds takes 2 or more parameters. !help for more info."
            )
        elif isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "For this command you have to be in a channel created by !ctf create."
            )
        elif isinstance(error.original, pymodm.errors.ValidationError):
            await ctx.channel.send("Malformatted URL...?")

    @ctf.command(aliases=[])
    async def startdate(self, ctx, *, date=None):
        """
        Sets the start date of the CTF
        """
        ctf = self._get_channel_ctf(ctx)
        if date:
            startdate = dateutil.parser.parse(date)
            if ctf.end_date and startdate >= ctf.end_date:
                raise DateMisconfiguredException

            ctf.start_date = startdate
            ctf.pending_reminders = []
            ctf.save()
            await success(ctx.message)
            await ctx.channel.send("Any reminders have been reset!")
        else:
            startdate = ctf.start_date
            if startdate is None:
                await ctx.channel.send(
                    "Ε αν βάλεις καμιάν ημερομηνία να σου την δείξω τζιόλας...!"
                )
            else:
                await ctx.channel.send("**Start time:** {0}".format(startdate))

    @ctf.command(aliases=[])
    async def enddate(self, ctx, *, date=None):
        """
        Sets the end date of the CTF
        """
        ctf = self._get_channel_ctf(ctx)
        if date:
            enddate = dateutil.parser.parse(date)

            if ctf.start_date and enddate <= ctf.start_date:
                raise DateMisconfiguredException

            ctf.end_date = enddate
            ctf.save()
            await success(ctx.message)
        else:
            enddate = ctf.end_date
            if enddate is None:
                await ctx.channel.send(
                    "Ε αν βάλεις καμιάν ημερομηνία να σου την δείξω τζιόλας...!"
                )
            else:
                await ctx.channel.send("**End time:** {0}".format(enddate))

    @enddate.error
    @startdate.error
    async def date_error(self, ctx, error):
        if isinstance(error.original, ValueError):
            await ctx.channel.send(
                "Ρε μα ενόμισες μυρίζουμε τα νύσσια μου? Βάλε ρε κουμπάρε μιά ημερομηνία του χαϊρκού!"
            )
        elif isinstance(error.original, DateMisconfiguredException):
            await ctx.channel.send(
                "Έκαμες τα σαλάτα με τις ημερομηνίες παλε... Πρέπει Start date > End Date"
            )
        elif isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "Ατού ο γαβρίλης.. For this command you have to be in a channel created by !ctf create."
            )

    @ctf.command()
    async def showcreds(self, ctx):
        """
        Displays shared credentials for the team
        """
        channel_name = str(ctx.channel.category)
        ctf = CTF.objects.get({"name": channel_name})
        if not (ctf.username and ctf.password):
            raise CTFSharedCredentialsNotSet
        emb = discord.Embed(description=ctf.credentials(), colour=4387968)
        await ctx.channel.send(embed=emb)
  
    @showcreds.error
    async def showcreds_error(self, ctx, error):
        if isinstance(error, CTF.DoesNotExist):
            await ctx.channel.send(
                "For this command you have to be in a channel created by !ctf create."
            )
        elif isinstance(error, CTFSharedCredentialsNotSet):
            await ctx.channel.send("This CTF has no shared credentials set!")

    @ctf.group()
    async def reminders(self, ctx):
        """
        Group of commands to manage reminders.
        Displays existing reminders if called without a subcommand.
        """
        if ctx.subcommand_passed is None:
            # No subcommand passed
            ctf = self._get_channel_ctf(ctx)
            if ctf.pending_reminders:
                await ctx.channel.send(
                    "\n".join(
                        "**{0})** {1}".format(idx + 1, r)
                        for idx, r in enumerate(ctf.pending_reminders)
                    )
                )
            else:
                await ctx.send(
                    "Εν έσσιει κανένα reminder ρε τσιάκκο... Εν να βάλεις όξα?"
                )
        elif ctx.invoked_subcommand is None:
            self.help_command.context = ctx
            await failed(ctx.message)
            await ctx.send(
                i18n._("**Invalid command passed**. See below for more help")
            )
            await self.help_command.command_callback(ctx, command=str(ctx.command))
            # subcomms = [sub_command for sub_command in ctx.command.all_commands]
            # await ctx.send(
            #     "Invalid command passed. Use !help for more info.\nAvailable subcommands are:\n```{0}```".format(
            #         " ".join(subcomms)
            #     )
            # )

    @ctf.command(name="countdown")
    async def countdown(self, ctx):
        """
        Displays countdown unitl CTF starts. Requires a start date to be set.
        @raises MissingStartDateException if ctf.start_date is None
        """
        ctf = self._get_channel_ctf(ctx)

        if not ctf.start_date:
            raise MissingStartDateException

        now = datetime.datetime.now()
        if now < ctf.start_date:
            await ctx.channel.send(
                "⏰   **" + td_format(ctf.start_date - now) + "** to start"
            )
        else:
            if ctf.end_date is None:
                await ctx.channel.send("Ρε παίχτη μου αρκεψεν... ξύπνα!")
            elif now < ctf.end_date:
                await ctx.channel.send(
                    "⏰   **" + td_format(ctf.end_date - now) + "** to finish"
                )
            else:
                await ctx.channel.send("Ρε παίχτη μου ετέλειωσεν... ξύπνα!")

    @reminders.command(name="add")
    async def reminders_add(self, ctx, unit, amount):
        """
        Adds a new reminder. Unit can be any time unit (hours, minutes, days...)
        Negative amount parameters means adda reminder before the ctf starts.
        """
        ctf = self._get_channel_ctf(ctx)

        if not ctf.start_date:
            raise MissingStartDateException

        amount = int(amount)
        # unit = unit
        # ?? unnesecary assignment to self
        td = timedelta(**{unit: amount})

        reminder_date = ctf.start_date + td
        ctf.pending_reminders.append(reminder_date)
        ctf.save()

        await success(ctx.message)

    @reminders_add.error
    @countdown.error
    async def reminders_add_error(self, ctx, error):
        if isinstance(error.original, (ValueError, TypeError)):
            await ctx.channel.send(
                "Εν να σε αφήκω να καταλάβεις μόνος σου τι μαλακία έκαμες..."
            )
        elif isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "For this command you have to be in a channel created by !ctf create."
            )
        elif isinstance(error.original, FewParametersException):
            await ctx.channel.send("This command takes more parameters. Use !help")
        elif isinstance(error.original, MissingStartDateException):
            await ctx.channel.send(
                "Φιλούδιν... πρέπει να βάλεις ενα start date (!ctf startdate ...) πρώτα!"
            )

    @reminders.command(name="rm", aliases=["remove"])
    async def reminders_rm(self, ctx, reminder_idx):
        """
        Removes a reminder from the reminder list.
        """
        ctf = self._get_channel_ctf(ctx)
        del ctf.pending_reminders[int(reminder_idx) - 1]
        ctf.save()
        await ctx.send("Reminder list updated!")

    @reminders_rm.error
    async def reminders_rm_error(self, ctx, error):
        if isinstance(error.original, IndexError):
            await ctx.channel.send(
                "Εν να σε αφήκω να καταλάβεις μόνος σου τι μαλακία έκαμες..."
            )
        elif isinstance(error.original, CTF.DoesNotExist):
            await ctx.channel.send(
                "For this command you have to be in a channel created by !ctf create."
            )
        elif isinstance(error.original, ValueError):
            await ctx.channel.send("Index μανα μου ... Index!")

    # @ctf.command()
    # async def reminders(self, ctx, param="auto"):
    #     ctf_obj = self._get_channel_ctf(ctx)
    # if param == "auto":
    #     upcoming_url = "https://ctftime.org/api/v1/events/"
    #     headers = {
    #         "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:61.0) Gecko/20100101 Firefox/61.0",
    #     }
    #     limit = 5
    #     data = requests.get(upcoming_url, headers=headers, params=str(limit)).json()
    #     date_for_reminder = ""
    #     ctf_found = False
    #     for ctf in data[:limit]:
    #         ctf_title = ctf["title"]
    #         if channel_name in ctf_title.lower():
    #             ctf_found = True
    #             date_for_reminder = ctf["start"]
    #             break

    #     if not ctf_found:
    #         raise CtfimeNameDoesNotMatch
    # else:
    #     _, date_for_reminder = dateutil.parser.parse(param), param

    # Save reminder to DB

    # ctf_obj.reminder = True
    # ctf_obj.date_for_reminder = date_for_reminder
    # ctf_obj.save()
    # await ctx.channel.send(
    #     f"Εν να σας θυμίσω τουλάχιστον μισή ώρα πριν αρκέψει το {ctf_title}."
    # )

    @tasks.loop(seconds=60)
    async def check_reminders(self):
        if len(self.bot.guilds) == 0:
            return
        REMINDERS_CHANNEL_ID = 579049835064197157
        guild = self.bot.guilds[0]
        reminders_channel = guild.get_channel(REMINDERS_CHANNEL_ID)
        ctfs = [c for c in guild.categories]
        for ctf in ctfs:
            try:
                ctf_doc = CTF.objects.get({"name": ctf.name})
                now = datetime.datetime.now()

                ## Check Custom Reminders
                if ctf_doc.start_date and ctf_doc.pending_reminders:
                    for reminder in copy.deepcopy(ctf_doc.pending_reminders):
                        if now >= reminder:
                            logger.info(
                                "Sending reminder to {0}".format(reminders_channel)
                            )
                            delta = abs(ctf_doc.start_date - now)
                            if now > ctf_doc.start_date:
                                reminder_text = "⏰ Ατέ ρε αχαμάκκιες... Εν να μπείτε? Το **{0}** άρκεψε εσσιει τζαί **{1}** λεπτά! ⏰"
                            else:
                                reminder_text = "⏰  Ντριιιινγκ... Ντριιινγκ!! Ατέ μανα μου, ξυπνάτε! Το **{0}** ξεκινά σε **{1}** λεπτά! ⏰"
                            await reminders_channel.send(
                                reminder_text.format(ctf.name, int(delta.seconds / 60))
                            )
                            ctf_doc.pending_reminders.remove(reminder)
                            ctf_doc.save()

                ## Check Start Reminders
                now_truncated = now.replace(second=0, microsecond=0)
                if (
                    ctf_doc.start_date
                    and ctf_doc.start_date.replace(second=0, microsecond=0)
                    == now_truncated
                ):
                    await reminders_channel.send(
                        "⛳ Το **{0}** άρκεψεν! Ταράσσετε εμπάτε! @here".format(
                            ctf_doc.name
                        )
                    )

                ## Check End Reminders
                now_truncated = now.replace(second=0, microsecond=0)
                if (
                    ctf_doc.end_date
                    and ctf_doc.end_date.replace(second=0, microsecond=0)
                    == now_truncated
                ):
                    await reminders_channel.send(
                        "⛳ Πάππαλλα το **{0}**! Τέλος! Εεε? Εδέραμε? @here ".format(
                            ctf_doc.name
                        )
                    )
            except CTF.DoesNotExist:
                continue


def setup(bot):
    bot.add_cog(Ctf(bot))
