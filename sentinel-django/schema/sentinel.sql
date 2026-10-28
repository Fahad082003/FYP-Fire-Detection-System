-- FireVision AI (Sentinel) database schema for SQL Server.
-- Generated from the EF Core migrations in backend/Sentinel.Api/Migrations with:
--   dotnet ef migrations script --project Sentinel.Api
-- (InitialCreate, FloorPlansAndIncidentLogs, CameraCalibration). It matches the live SentinelDb as of 2026-10-26.
-- Used by `manage.py create_schema` (fresh installs) and by the test runner. Never run it on an existing database.

IF OBJECT_ID(N'[__EFMigrationsHistory]') IS NULL
BEGIN
    CREATE TABLE [__EFMigrationsHistory] (
        [MigrationId] nvarchar(150) NOT NULL,
        [ProductVersion] nvarchar(32) NOT NULL,
        CONSTRAINT [PK___EFMigrationsHistory] PRIMARY KEY ([MigrationId])
    );
END;
GO

BEGIN TRANSACTION;
GO

CREATE TABLE [CameraFeeds] (
    [Id] nvarchar(64) NOT NULL,
    [Name] nvarchar(120) NOT NULL,
    [FeedType] nvarchar(16) NOT NULL,
    [StreamUrl] nvarchar(500) NOT NULL,
    [StreamFormat] nvarchar(16) NOT NULL,
    [Status] nvarchar(16) NOT NULL,
    [LocationZone] nvarchar(64) NOT NULL,
    [CreatedAt] datetime2 NOT NULL,
    [UpdatedAt] datetime2 NOT NULL,
    CONSTRAINT [PK_CameraFeeds] PRIMARY KEY ([Id])
);
GO

CREATE TABLE [SensorNodes] (
    [Id] nvarchar(64) NOT NULL,
    [Type] nvarchar(16) NOT NULL,
    [CurrentValue] float NOT NULL,
    [Threshold] float NOT NULL,
    [LocationZone] nvarchar(64) NOT NULL,
    [LastReadingAt] datetime2 NULL,
    [UpdatedAt] datetime2 NOT NULL,
    CONSTRAINT [PK_SensorNodes] PRIMARY KEY ([Id]),
    CONSTRAINT [CK_SensorNodes_Threshold] CHECK ([Threshold] > 0)
);
GO

CREATE TABLE [SmartSprinklers] (
    [Id] nvarchar(64) NOT NULL,
    [Zone] nvarchar(64) NOT NULL,
    [IsActive] bit NOT NULL,
    [Mode] nvarchar(16) NOT NULL,
    [LastTriggeredAt] datetime2 NULL,
    [UpdatedAt] datetime2 NOT NULL,
    CONSTRAINT [PK_SmartSprinklers] PRIMARY KEY ([Id])
);
GO

CREATE TABLE [Users] (
    [Id] int NOT NULL IDENTITY,
    [Email] nvarchar(256) NOT NULL,
    [DisplayName] nvarchar(120) NOT NULL,
    [PasswordHash] nvarchar(512) NOT NULL,
    [Role] nvarchar(16) NOT NULL,
    [IsActive] bit NOT NULL,
    [CreatedAt] datetime2 NOT NULL,
    [LastLoginAt] datetime2 NULL,
    CONSTRAINT [PK_Users] PRIMARY KEY ([Id])
);
GO

CREATE TABLE [IncidentAlerts] (
    [Id] int NOT NULL IDENTITY,
    [Timestamp] datetime2 NOT NULL,
    [UpdatedAt] datetime2 NOT NULL,
    [CameraId] nvarchar(64) NULL,
    [SensorId] nvarchar(64) NULL,
    [Source] nvarchar(16) NOT NULL,
    [Zone] nvarchar(64) NOT NULL,
    [Severity] nvarchar(16) NOT NULL,
    [FireDetected] bit NOT NULL,
    [FireSeverity] nvarchar(16) NULL,
    [HumanCount] int NOT NULL,
    [DangerCount] int NOT NULL,
    [Message] nvarchar(500) NOT NULL,
    [IsResolved] bit NOT NULL,
    [ResolvedAt] datetime2 NULL,
    CONSTRAINT [PK_IncidentAlerts] PRIMARY KEY ([Id]),
    CONSTRAINT [FK_IncidentAlerts_CameraFeeds_CameraId] FOREIGN KEY ([CameraId]) REFERENCES [CameraFeeds] ([Id]) ON DELETE SET NULL,
    CONSTRAINT [FK_IncidentAlerts_SensorNodes_SensorId] FOREIGN KEY ([SensorId]) REFERENCES [SensorNodes] ([Id]) ON DELETE SET NULL
);
GO

CREATE TABLE [SensorReadings] (
    [Id] bigint NOT NULL IDENTITY,
    [SensorId] nvarchar(64) NOT NULL,
    [Value] float NOT NULL,
    [RecordedAt] datetime2 NOT NULL,
    CONSTRAINT [PK_SensorReadings] PRIMARY KEY ([Id]),
    CONSTRAINT [FK_SensorReadings_SensorNodes_SensorId] FOREIGN KEY ([SensorId]) REFERENCES [SensorNodes] ([Id]) ON DELETE CASCADE
);
GO

CREATE TABLE [PushSubscriptions] (
    [Id] int NOT NULL IDENTITY,
    [Endpoint] nvarchar(500) NOT NULL,
    [P256dh] nvarchar(255) NOT NULL,
    [Auth] nvarchar(255) NOT NULL,
    [UserId] int NULL,
    [UserAgent] nvarchar(255) NOT NULL,
    [IsActive] bit NOT NULL,
    [CreatedAt] datetime2 NOT NULL,
    CONSTRAINT [PK_PushSubscriptions] PRIMARY KEY ([Id]),
    CONSTRAINT [FK_PushSubscriptions_Users_UserId] FOREIGN KEY ([UserId]) REFERENCES [Users] ([Id]) ON DELETE CASCADE
);
GO

CREATE INDEX [IX_CameraFeeds_LocationZone] ON [CameraFeeds] ([LocationZone]);
GO

CREATE INDEX [IX_IncidentAlerts_CameraId] ON [IncidentAlerts] ([CameraId]);
GO

CREATE INDEX [IX_IncidentAlerts_IsResolved_Timestamp] ON [IncidentAlerts] ([IsResolved], [Timestamp]);
GO

CREATE INDEX [IX_IncidentAlerts_SensorId] ON [IncidentAlerts] ([SensorId]);
GO

CREATE INDEX [IX_IncidentAlerts_Zone] ON [IncidentAlerts] ([Zone]);
GO

CREATE UNIQUE INDEX [IX_PushSubscriptions_Endpoint] ON [PushSubscriptions] ([Endpoint]);
GO

CREATE INDEX [IX_PushSubscriptions_UserId] ON [PushSubscriptions] ([UserId]);
GO

CREATE INDEX [IX_SensorNodes_LocationZone] ON [SensorNodes] ([LocationZone]);
GO

CREATE INDEX [IX_SensorReadings_RecordedAt] ON [SensorReadings] ([RecordedAt]);
GO

CREATE INDEX [IX_SensorReadings_SensorId_RecordedAt] ON [SensorReadings] ([SensorId], [RecordedAt]);
GO

CREATE INDEX [IX_SmartSprinklers_Zone] ON [SmartSprinklers] ([Zone]);
GO

CREATE UNIQUE INDEX [IX_Users_Email] ON [Users] ([Email]);
GO

INSERT INTO [__EFMigrationsHistory] ([MigrationId], [ProductVersion])
VALUES (N'20260930145518_InitialCreate', N'8.0.31');
GO

COMMIT;
GO

BEGIN TRANSACTION;
GO

CREATE TABLE [FloorPlans] (
    [Id] int NOT NULL IDENTITY,
    [Name] nvarchar(120) NOT NULL,
    [Building] nvarchar(120) NOT NULL,
    [FloorNumber] int NOT NULL,
    [WidthMeters] float NOT NULL,
    [HeightMeters] float NOT NULL,
    [ImageFileName] nvarchar(64) NULL,
    [CreatedAt] datetime2 NOT NULL,
    [UpdatedAt] datetime2 NOT NULL,
    CONSTRAINT [PK_FloorPlans] PRIMARY KEY ([Id])
);
GO

CREATE TABLE [IncidentLogs] (
    [Id] bigint NOT NULL IDENTITY,
    [IncidentId] int NULL,
    [Type] nvarchar(16) NOT NULL,
    [Timestamp] datetime2 NOT NULL,
    [Zone] nvarchar(120) NOT NULL,
    [Details] nvarchar(500) NOT NULL,
    [FireAreaPercent] float NULL,
    [FireSeverity] nvarchar(16) NULL,
    [HumanCount] int NULL,
    [DangerCount] int NULL,
    [PersonTrackId] nvarchar(48) NULL,
    [PersonX] float NULL,
    [PersonY] float NULL,
    [DangerZone] nvarchar(48) NULL,
    [Simulated] bit NOT NULL,
    CONSTRAINT [PK_IncidentLogs] PRIMARY KEY ([Id]),
    CONSTRAINT [FK_IncidentLogs_IncidentAlerts_IncidentId] FOREIGN KEY ([IncidentId]) REFERENCES [IncidentAlerts] ([Id]) ON DELETE CASCADE
);
GO

CREATE TABLE [FloorPlanElements] (
    [Id] int NOT NULL IDENTITY,
    [FloorPlanId] int NOT NULL,
    [Type] nvarchar(16) NOT NULL,
    [Name] nvarchar(80) NOT NULL,
    [X] float NOT NULL,
    [Y] float NOT NULL,
    [Width] float NOT NULL,
    [Height] float NOT NULL,
    [IsBlocked] bit NOT NULL,
    [IsActive] bit NOT NULL,
    [Zone] nvarchar(64) NULL,
    CONSTRAINT [PK_FloorPlanElements] PRIMARY KEY ([Id]),
    CONSTRAINT [FK_FloorPlanElements_FloorPlans_FloorPlanId] FOREIGN KEY ([FloorPlanId]) REFERENCES [FloorPlans] ([Id]) ON DELETE CASCADE
);
GO

CREATE INDEX [IX_FloorPlanElements_FloorPlanId] ON [FloorPlanElements] ([FloorPlanId]);
GO

CREATE INDEX [IX_IncidentLogs_IncidentId_Timestamp] ON [IncidentLogs] ([IncidentId], [Timestamp]);
GO

CREATE INDEX [IX_IncidentLogs_Type_Timestamp] ON [IncidentLogs] ([Type], [Timestamp]);
GO

INSERT INTO [__EFMigrationsHistory] ([MigrationId], [ProductVersion])
VALUES (N'20261012171824_FloorPlansAndIncidentLogs', N'8.0.31');
GO

COMMIT;
GO

BEGIN TRANSACTION;
GO

ALTER TABLE [CameraFeeds] ADD [Calibration] nvarchar(2000) NULL;
GO

INSERT INTO [__EFMigrationsHistory] ([MigrationId], [ProductVersion])
VALUES (N'20261015132603_CameraCalibration', N'8.0.31');
GO

COMMIT;
GO

