from asgiref.sync import sync_to_async
from django.db import models

from activities.models.timeline_event import TimelineEvent
from core.ld import canonicalise
from stator.models import State, StateField, StateGraph, StatorModel


class FanOutStates(StateGraph):
    new = State(try_interval=300)
    sent = State()

    new.transitions_to(sent)

    @classmethod
    async def handle_new(cls, instance: "FanOut"):
        """
        Sends the fan-out to the right inbox.
        """

        fan_out = await instance.afetch_full()

        match (fan_out.type, fan_out.identity.local):
            # Handle creating/updating local posts
            case ((FanOut.Types.post | FanOut.Types.post_edited), True):
                post = await fan_out.subject_post.afetch_full()
                # Make a timeline event directly
                # If it's a reply, we only add it if we follow at least one
                # of the people mentioned AND the author
                add = True
                mentioned = {identity.id for identity in post.mentions.all()}
                followed = await sync_to_async(set)(
                    fan_out.identity.outbound_follows.values_list("id", flat=True)
                )
                if post.in_reply_to:
                    add = (post.author_id in followed) and bool(
                        mentioned.intersection(followed)
                    )
                if add:
                    await sync_to_async(TimelineEvent.add_post)(
                        identity=fan_out.identity,
                        post=post,
                    )
                # We might have been mentioned
                if fan_out.identity.id in mentioned:
                    await sync_to_async(TimelineEvent.add_mentioned)(
                        identity=fan_out.identity,
                        post=post,
                    )

            # Handle sending remote posts create
            case (FanOut.Types.post, False):
                post = await fan_out.subject_post.afetch_full()
                # Sign it and send it
                await post.author.signed_request(
                    method="post",
                    uri=fan_out.identity.inbox_uri,
                    body=canonicalise(post.to_create_ap()),
                )

            # Handle sending remote posts update
            case (FanOut.Types.post_edited, False):
                post = await fan_out.subject_post.afetch_full()
                # Sign it and send it
                await post.author.signed_request(
                    method="post",
                    uri=fan_out.identity.inbox_uri,
                    body=canonicalise(post.to_update_ap()),
                )

            # Handle deleting local posts
            case (FanOut.Types.post_deleted, True):
                post = await fan_out.subject_post.afetch_full()
                if fan_out.identity.local:
                    # Remove all timeline events mentioning it
                    await TimelineEvent.objects.filter(
                        identity=fan_out.identity,
                        subject_post=post,
                    ).adelete()

            # Handle sending remote post deletes
            case (FanOut.Types.post_deleted, False):
                post = await fan_out.subject_post.afetch_full()
                # Send it to the remote inbox
                await post.author.signed_request(
                    method="post",
                    uri=fan_out.identity.inbox_uri,
                    body=canonicalise(post.to_delete_ap()),
                )

            # Handle local boosts/likes
            case (FanOut.Types.interaction, True):
                interaction = await fan_out.subject_post_interaction.afetch_full()
                # Make a timeline event directly
                await sync_to_async(TimelineEvent.add_post_interaction)(
                    identity=fan_out.identity,
                    interaction=interaction,
                )

            # Handle sending remote boosts/likes
            case (FanOut.Types.interaction, False):
                interaction = await fan_out.subject_post_interaction.afetch_full()
                # Send it to the remote inbox
                await interaction.identity.signed_request(
                    method="post",
                    uri=fan_out.identity.inbox_uri,
                    body=canonicalise(interaction.to_ap()),
                )

            # Handle undoing local boosts/likes
            case (FanOut.Types.undo_interaction, True):  # noqa:F841
                interaction = await fan_out.subject_post_interaction.afetch_full()

                # Delete any local timeline events
                await sync_to_async(TimelineEvent.delete_post_interaction)(
                    identity=fan_out.identity,
                    interaction=interaction,
                )

            # Handle sending remote undoing boosts/likes
            case (FanOut.Types.undo_interaction, False):  # noqa:F841
                interaction = await fan_out.subject_post_interaction.afetch_full()
                # Send an undo to the remote inbox
                await interaction.identity.signed_request(
                    method="post",
                    uri=fan_out.identity.inbox_uri,
                    body=canonicalise(interaction.to_undo_ap()),
                )

            case _:
                raise ValueError(
                    f"Cannot fan out with type {fan_out.type} local={fan_out.identity.local}"
                )

        return cls.sent


class FanOut(StatorModel):
    """
    An activity that needs to get to an inbox somewhere.
    """

    class Types(models.TextChoices):
        post = "post"
        post_edited = "post_edited"
        post_deleted = "post_deleted"
        interaction = "interaction"
        undo_interaction = "undo_interaction"

    state = StateField(FanOutStates)

    # The user this event is targeted at
    identity = models.ForeignKey(
        "users.Identity",
        on_delete=models.CASCADE,
        related_name="fan_outs",
    )

    # What type of activity it is
    type = models.CharField(max_length=100, choices=Types.choices)

    # Links to the appropriate objects
    subject_post = models.ForeignKey(
        "activities.Post",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="fan_outs",
    )
    subject_post_interaction = models.ForeignKey(
        "activities.PostInteraction",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="fan_outs",
    )

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    ### Async helpers ###

    async def afetch_full(self):
        """
        Returns a version of the object with all relations pre-loaded
        """
        return await FanOut.objects.select_related(
            "identity",
            "subject_post",
            "subject_post_interaction",
        ).aget(pk=self.pk)
