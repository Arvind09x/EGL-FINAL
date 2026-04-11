"""
Signals for auto-creating BorrowerProfile on user creation.
Covers tasks M1-2 and A2-3.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.signals import user_logged_in
from django.conf import settings

from .models import (
    BorrowerProfile,
    ClubSocietyEmailAllowlist,
    RAEmailAllowlist,
    User,
)


@receiver(post_save, sender=User)
def create_borrower_profile(sender, instance, created, **kwargs):
    """Auto-create a BorrowerProfile on first user creation.
    
    Detects RA/club emails from preloaded allowlists and sets policy_type.
    """
    if created:
        policy_type = BorrowerProfile.PolicyType.INDIVIDUAL
        email = instance.email.lower()

        if RAEmailAllowlist.objects.filter(email__iexact=email).exists():
            policy_type = BorrowerProfile.PolicyType.RA
        elif ClubSocietyEmailAllowlist.objects.filter(email__iexact=email).exists():
            policy_type = BorrowerProfile.PolicyType.CLUB_SOCIETY
        elif instance.is_member:
            policy_type = BorrowerProfile.PolicyType.EESHTO_MEMBER

        BorrowerProfile.objects.get_or_create(
            user=instance,
            defaults={"policy_type": policy_type},
        )

@receiver(user_logged_in)
def auto_promote_test_superusers(sender, request, user, **kwargs):
    """
    If the user's email is in settings.TEST_SUPERUSERS, automatically grant
    them superuser and member desk privileges upon login.
    """
    test_superusers = getattr(settings, 'TEST_SUPERUSERS', [])
    if user.email in test_superusers:
        changed = False
        if not user.is_superuser:
            user.is_superuser = True
            changed = True
        if not user.is_staff:
            user.is_staff = True
            changed = True
        if not getattr(user, 'is_senior_admin', False):
            user.is_senior_admin = True
            changed = True
        if not getattr(user, 'is_member', False):
            user.is_member = True
            changed = True
            
        if changed:
            user.save()
