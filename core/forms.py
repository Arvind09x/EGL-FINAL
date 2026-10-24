"""Forms for the ESHTO Games Library."""

from django import forms

from .models import WeeklySlot
from .services import validate_slot_times

_INPUT = (
    "w-full px-4 py-3 rounded-xl bg-surface-50 border border-surface-300 "
    "text-surface-800 placeholder-surface-400 focus:border-red-500 "
    "focus:outline-none focus:ring-2 focus:ring-red-500/20 transition-all"
)


class SlotForm(forms.ModelForm):
    """Create or edit a borrow/return slot (any Eeshto member)."""

    repeat_weeks = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=26,
        initial=0,
        label="Repeat weekly for N more weeks",
        widget=forms.NumberInput(attrs={"class": _INPUT, "min": 0, "max": 26}),
    )

    class Meta:
        model = WeeklySlot
        fields = ["slot_type", "date", "start_time", "end_time", "notes"]
        widgets = {
            "slot_type": forms.Select(attrs={"class": _INPUT}),
            "date": forms.DateInput(attrs={"type": "date", "class": _INPUT}, format="%Y-%m-%d"),
            "start_time": forms.TimeInput(attrs={"type": "time", "class": _INPUT}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time", "class": _INPUT}, format="%H:%M"),
            "notes": forms.TextInput(
                attrs={"class": _INPUT, "placeholder": "Optional note shown to members"}
            ),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if start and end:
            error = validate_slot_times(start, end)
            if error:
                self.add_error("end_time", error)
        return cleaned
