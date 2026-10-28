-- One-time: renames the old demo zones (Zone A/B/C) to "Floor - Area" names in an existing SentinelDb.
-- New databases get these names from the seed already. Stop the API first, then:
--   sqlcmd -S localhost -E -C -d SentinelDb -i database\rename-zones.sql
SET XACT_ABORT ON;
BEGIN TRANSACTION;
DECLARE @map TABLE (Old nvarchar(64), New nvarchar(64));
INSERT @map VALUES (N'Zone A', N'Ground Floor - Lobby'), (N'Zone B', N'Ground Floor - Warehouse'), (N'Zone C', N'First Floor - Server Hall');

UPDATE t SET LocationZone = m.New FROM CameraFeeds t JOIN @map m ON t.LocationZone = m.Old;
UPDATE t SET LocationZone = m.New FROM SensorNodes t JOIN @map m ON t.LocationZone = m.Old;
UPDATE t SET Zone = m.New FROM SmartSprinklers t JOIN @map m ON t.Zone = m.Old;
UPDATE t SET Zone = m.New FROM FloorPlanElements t JOIN @map m ON t.Zone = m.Old;
-- Old incidents too, so history and open incidents still match their sprinklers.
UPDATE t SET Zone = m.New FROM IncidentAlerts t JOIN @map m ON t.Zone = m.Old;
COMMIT;
SELECT LocationZone AS Zone, COUNT(*) AS Cameras FROM CameraFeeds GROUP BY LocationZone;
