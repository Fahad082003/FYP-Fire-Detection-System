"""Mappings onto the existing SQL Server tables created by the ASP.NET Core / EF Core migrations.

Every model is unmanaged: Django never creates or alters these tables. Table and column names, lengths and enum
values (stored as text, e.g. "Critical") are exactly the EF Core ones, so both stacks can share SentinelDb.
The schema itself lives in schema/sentinel.sql. Foreign keys use DO_NOTHING because the database enforces
the cascade rules (ON DELETE SET NULL / CASCADE), as it did for EF Core.
"""
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone


class FeedType(models.TextChoices):
    CCTV = "CCTV"
    DRONE = "Drone"


class StreamFormat(models.TextChoices):
    SIMULATED = "Simulated"
    MJPEG = "Mjpeg"
    HLS = "Hls"
    MP4 = "Mp4"


class CameraStatus(models.TextChoices):
    ONLINE = "Online"
    DEGRADED = "Degraded"
    OFFLINE = "Offline"


class SensorType(models.TextChoices):
    SMOKE = "Smoke"
    TEMPERATURE = "Temperature"


class SprinklerMode(models.TextChoices):
    AUTO = "Auto"
    MANUAL = "Manual"


class IncidentSource(models.TextChoices):
    VISION = "Vision"
    SENSOR = "Sensor"
    MANUAL = "Manual"
    CAMERA_LOST = "CameraLost"
    SENSOR_LOST = "SensorLost"  # fits the existing nvarchar(16) column: no schema change


class UserRole(models.TextChoices):
    ADMIN = "Admin"
    OPERATOR = "Operator"


class FloorElementType(models.TextChoices):
    ROOM = "Room"
    CORRIDOR = "Corridor"
    WALL = "Wall"
    DOOR = "Door"
    EXIT = "Exit"
    SAFE_ZONE = "SafeZone"
    DANGER_ZONE = "DangerZone"


class IncidentLogType(models.TextChoices):
    OPENED = "Opened"
    ESCALATED = "Escalated"
    RESOLVED = "Resolved"
    FIRE_MEASUREMENT = "FireMeasurement"
    PERSON_IN_DANGER = "PersonInDanger"
    EVACUATION_ROUTE = "EvacuationRoute"
    FIRE_GROWTH = "FireGrowth"  # fits the existing nvarchar(16) column: no schema change
    RISK_WARNING = "RiskWarning"  # same column, no schema change
    FALSE_ALARM = "FalseAlarm"  # operator's verdict on an incident; same column, no schema change
    SENSOR_RECOVERED = "SensorRecovered"  # a stale sensor reports again; same column, no schema change
    CORROBORATED = "Corroborated"  # a smoke sensor in the zone confirms an AI fire; same column, no schema change


SEVERITIES = ["Low", "Medium", "High", "Critical"]  # ordered: index = rank


def rank(severity: str | None) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else -1


class AppUserManager(BaseUserManager):
    def get_by_natural_key(self, email):
        return self.get(email=email.strip().lower())


class AppUser(AbstractBaseUser):
    """The existing Users table. Passwords stay in the ASP.NET Identity format (see security.py)."""
    id = models.AutoField(primary_key=True, db_column="Id")
    email = models.CharField(max_length=256, unique=True, db_column="Email")
    display_name = models.CharField(max_length=120, db_column="DisplayName")
    password = models.CharField(max_length=512, db_column="PasswordHash")
    role = models.CharField(max_length=16, choices=UserRole.choices, default=UserRole.OPERATOR, db_column="Role")
    is_active = models.BooleanField(default=True, db_column="IsActive")
    created_at = models.DateTimeField(default=timezone.now, db_column="CreatedAt")
    last_login = models.DateTimeField(null=True, blank=True, db_column="LastLoginAt")

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    objects = AppUserManager()

    class Meta:
        managed = False
        db_table = "Users"

    def set_password(self, raw_password):
        from .security import hash_password
        self.password = hash_password(raw_password)

    def check_password(self, raw_password):
        from .security import verify_password
        return verify_password(self.password, raw_password)

    @property
    def is_admin(self):
        return self.role == UserRole.ADMIN


class CameraFeed(models.Model):
    id = models.CharField(max_length=64, primary_key=True, db_column="Id")  # the id the AI service posts as cameraId
    name = models.CharField(max_length=120, db_column="Name")
    feed_type = models.CharField(max_length=16, choices=FeedType.choices, default=FeedType.CCTV, db_column="FeedType")
    stream_url = models.CharField(max_length=500, blank=True, default="", db_column="StreamUrl")
    stream_format = models.CharField(max_length=16, choices=StreamFormat.choices, default=StreamFormat.SIMULATED, db_column="StreamFormat")
    status = models.CharField(max_length=16, choices=CameraStatus.choices, default=CameraStatus.ONLINE, db_column="Status")
    location_zone = models.CharField(max_length=64, db_column="LocationZone")
    calibration = models.CharField(max_length=2000, null=True, blank=True, db_column="Calibration")  # JSON, null = not calibrated
    created_at = models.DateTimeField(default=timezone.now, db_column="CreatedAt")
    updated_at = models.DateTimeField(default=timezone.now, db_column="UpdatedAt")

    class Meta:
        managed = False
        db_table = "CameraFeeds"


class SensorNode(models.Model):
    id = models.CharField(max_length=64, primary_key=True, db_column="Id")
    type = models.CharField(max_length=16, choices=SensorType.choices, db_column="Type")
    current_value = models.FloatField(default=0, db_column="CurrentValue")
    threshold = models.FloatField(db_column="Threshold")
    location_zone = models.CharField(max_length=64, db_column="LocationZone")
    last_reading_at = models.DateTimeField(null=True, blank=True, db_column="LastReadingAt")
    updated_at = models.DateTimeField(default=timezone.now, db_column="UpdatedAt")

    class Meta:
        managed = False
        db_table = "SensorNodes"

    @property
    def unit(self):
        return "ppm" if self.type == SensorType.SMOKE else "°C"


class SensorReading(models.Model):
    id = models.BigAutoField(primary_key=True, db_column="Id")
    sensor = models.ForeignKey(SensorNode, models.DO_NOTHING, db_column="SensorId", related_name="readings")
    value = models.FloatField(db_column="Value")
    recorded_at = models.DateTimeField(default=timezone.now, db_column="RecordedAt")

    class Meta:
        managed = False
        db_table = "SensorReadings"


class SmartSprinkler(models.Model):
    id = models.CharField(max_length=64, primary_key=True, db_column="Id")
    zone = models.CharField(max_length=64, db_column="Zone")
    is_active = models.BooleanField(default=False, db_column="IsActive")
    mode = models.CharField(max_length=16, choices=SprinklerMode.choices, default=SprinklerMode.AUTO, db_column="Mode")
    last_triggered_at = models.DateTimeField(null=True, blank=True, db_column="LastTriggeredAt")
    updated_at = models.DateTimeField(default=timezone.now, db_column="UpdatedAt")

    class Meta:
        managed = False
        db_table = "SmartSprinklers"


class IncidentAlert(models.Model):
    id = models.AutoField(primary_key=True, db_column="Id")
    timestamp = models.DateTimeField(default=timezone.now, db_column="Timestamp")
    updated_at = models.DateTimeField(default=timezone.now, db_column="UpdatedAt")
    camera = models.ForeignKey(CameraFeed, models.DO_NOTHING, null=True, blank=True, db_column="CameraId", db_constraint=False)
    sensor = models.ForeignKey(SensorNode, models.DO_NOTHING, null=True, blank=True, db_column="SensorId", db_constraint=False)
    source = models.CharField(max_length=16, choices=IncidentSource.choices, default=IncidentSource.VISION, db_column="Source")
    zone = models.CharField(max_length=64, db_column="Zone")
    severity = models.CharField(max_length=16, default="Low", db_column="Severity")  # only escalates while open = peak
    fire_detected = models.BooleanField(default=False, db_column="FireDetected")
    fire_severity = models.CharField(max_length=16, null=True, blank=True, db_column="FireSeverity")
    human_count = models.IntegerField(default=0, db_column="HumanCount")
    danger_count = models.IntegerField(default=0, db_column="DangerCount")
    message = models.CharField(max_length=500, default="", db_column="Message")
    is_resolved = models.BooleanField(default=False, db_column="IsResolved")
    resolved_at = models.DateTimeField(null=True, blank=True, db_column="ResolvedAt")

    class Meta:
        managed = False
        db_table = "IncidentAlerts"


class IncidentLog(models.Model):
    """History: lifecycle, fire measurements, person-in-danger alerts and evacuation-route estimates."""
    id = models.BigAutoField(primary_key=True, db_column="Id")
    incident = models.ForeignKey(IncidentAlert, models.DO_NOTHING, null=True, blank=True, db_column="IncidentId")
    type = models.CharField(max_length=16, choices=IncidentLogType.choices, db_column="Type")
    timestamp = models.DateTimeField(default=timezone.now, db_column="Timestamp")
    zone = models.CharField(max_length=120, default="", db_column="Zone")
    details = models.CharField(max_length=500, default="", db_column="Details")
    fire_area_percent = models.FloatField(null=True, blank=True, db_column="FireAreaPercent")  # % of frame, not a physical size
    fire_severity = models.CharField(max_length=16, null=True, blank=True, db_column="FireSeverity")
    human_count = models.IntegerField(null=True, blank=True, db_column="HumanCount")
    danger_count = models.IntegerField(null=True, blank=True, db_column="DangerCount")
    person_track_id = models.CharField(max_length=48, null=True, blank=True, db_column="PersonTrackId")
    person_x = models.FloatField(null=True, blank=True, db_column="PersonX")
    person_y = models.FloatField(null=True, blank=True, db_column="PersonY")
    danger_zone = models.CharField(max_length=48, null=True, blank=True, db_column="DangerZone")
    simulated = models.BooleanField(default=False, db_column="Simulated")

    class Meta:
        managed = False
        db_table = "IncidentLogs"


class FloorPlan(models.Model):
    """One building floor. All element coordinates are meters, origin top-left."""
    id = models.AutoField(primary_key=True, db_column="Id")
    name = models.CharField(max_length=120, db_column="Name")
    building = models.CharField(max_length=120, default="", blank=True, db_column="Building")
    floor_number = models.IntegerField(default=0, db_column="FloorNumber")
    width_meters = models.FloatField(db_column="WidthMeters")
    height_meters = models.FloatField(db_column="HeightMeters")
    image_file_name = models.CharField(max_length=64, null=True, blank=True, db_column="ImageFileName")
    created_at = models.DateTimeField(default=timezone.now, db_column="CreatedAt")
    updated_at = models.DateTimeField(default=timezone.now, db_column="UpdatedAt")

    class Meta:
        managed = False
        db_table = "FloorPlans"


class FloorPlanElement(models.Model):
    """Axis-aligned rectangle on a floor plan: room, corridor, wall, door, exit, safe zone or danger zone."""
    id = models.AutoField(primary_key=True, db_column="Id")
    floor_plan = models.ForeignKey(FloorPlan, models.DO_NOTHING, db_column="FloorPlanId", related_name="elements")
    type = models.CharField(max_length=16, choices=FloorElementType.choices, db_column="Type")
    name = models.CharField(max_length=80, default="", blank=True, db_column="Name")
    x = models.FloatField(db_column="X")
    y = models.FloatField(db_column="Y")
    width = models.FloatField(db_column="Width")
    height = models.FloatField(db_column="Height")
    is_blocked = models.BooleanField(default=False, db_column="IsBlocked")  # doors/exits: closed, routes may not pass
    is_active = models.BooleanField(default=False, db_column="IsActive")  # danger zones: manually marked dangerous
    zone = models.CharField(max_length=64, null=True, blank=True, db_column="Zone")  # danger zones: linked camera/sensor zone

    class Meta:
        managed = False
        db_table = "FloorPlanElements"


class WebPushSubscription(models.Model):
    id = models.AutoField(primary_key=True, db_column="Id")
    endpoint = models.CharField(max_length=500, unique=True, db_column="Endpoint")
    p256dh = models.CharField(max_length=255, db_column="P256dh")
    auth = models.CharField(max_length=255, db_column="Auth")
    user = models.ForeignKey(AppUser, models.DO_NOTHING, null=True, blank=True, db_column="UserId")
    user_agent = models.CharField(max_length=255, default="", blank=True, db_column="UserAgent")
    is_active = models.BooleanField(default=True, db_column="IsActive")
    created_at = models.DateTimeField(default=timezone.now, db_column="CreatedAt")

    class Meta:
        managed = False
        db_table = "PushSubscriptions"
