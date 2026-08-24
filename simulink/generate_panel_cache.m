%% generate_panel_cache.m
%
% Generate a SQLite cache containing the relevant information for every
% Simscape Electrical Solar Cell part.
%
% One panel = one row in `panels`, plus related rows in
% `temperature_characteristics`, `irradiance_characteristics`, and
% `iv_curve`.
%

clear;
clc;

%% Resolve paths relative to this script's own location

scriptDir = fileparts(mfilename('fullpath'));

%% Configuration

block = "PVOutputSimulation2025a/Solar Cell";

collectionId = "SimscapeElectrical";

outputFile = fullfile(scriptDir, '..', 'data', 'raw', 'panel_cache.db');

% MathWorks datasheet-generator temperature points
temperatureVec = [-10 0 20 45];

% MathWorks datasheet-generator irradiance points
irradianceFractions = [0.4 0.6 0.8 1.0];


%% Load Simulink model
% Loaded by full path (not bare name) to avoid ambiguity if another
% file named PVOutputSimulation2025a.slx exists elsewhere on the
% MATLAB path. The block path in `block` only resolves once the model
% is actually loaded into the session.

load_system(fullfile(scriptDir, 'PVOutputSimulation2025a.slx'));

%% Solar Cell parts

parts = partrepo.collection.listDataSetsForBlock(block);

partIds = parts.PartId;

%% Create / overwrite cache

if isfile(outputFile)
    delete(outputFile);
end

conn = sqlite(outputFile, "create");

execute(conn, [ ...
    "CREATE TABLE panels (" + ...
    "id INTEGER PRIMARY KEY, " + ...
    "part_id TEXT NOT NULL UNIQUE, " + ...
    "manufacturer TEXT NOT NULL, " + ...
    "model_name TEXT NOT NULL, " + ...
    "n_series REAL, " + ...
    "n_parallel REAL, " + ...
    "rs REAL, " + ...
    "rp REAL, " + ...
    "tfixed REAL, " + ...
    "vmp REAL, " + ...
    "imp REAL, " + ...
    "pmp REAL, " + ...
    "voc REAL, " + ...
    "isc REAL, " + ...
    "ir0 REAL, " + ...
    "tmeas REAL)"]);

execute(conn, [ ...
    "CREATE TABLE temperature_characteristics (" + ...
    "panel_id INTEGER NOT NULL, " + ...
    "irradiance_wperm2 REAL NOT NULL, " + ...
    "temperature_degC REAL NOT NULL, " + ...
    "voc_volts REAL, " + ...
    "isc_ampere REAL, " + ...
    "vm_volts REAL, " + ...
    "im_ampere REAL, " + ...
    "pm_watts REAL, " + ...
    "FOREIGN KEY (panel_id) REFERENCES panels(id))"]);

execute(conn, [ ...
    "CREATE TABLE irradiance_characteristics (" + ...
    "panel_id INTEGER NOT NULL, " + ...
    "irradiance_wperm2 REAL NOT NULL, " + ...
    "temperature_degC REAL NOT NULL, " + ...
    "voc_volts REAL, " + ...
    "isc_ampere REAL, " + ...
    "vm_volts REAL, " + ...
    "im_ampere REAL, " + ...
    "pm_watts REAL, " + ...
    "FOREIGN KEY (panel_id) REFERENCES panels(id))"]);

execute(conn, [ ...
    "CREATE TABLE iv_curve (" + ...
    "panel_id INTEGER NOT NULL, " + ...
    "point INTEGER NOT NULL, " + ...
    "voltage REAL NOT NULL, " + ...
    "current REAL NOT NULL, " + ...
    "power REAL NOT NULL, " + ...
    "PRIMARY KEY (panel_id, point), " + ...
    "FOREIGN KEY (panel_id) REFERENCES panels(id))"]);

execute(conn, "CREATE INDEX idx_panels_manufacturer ON panels(manufacturer)");
execute(conn, "CREATE INDEX idx_panels_model ON panels(model_name)");
execute(conn, "CREATE INDEX idx_panels_part_id ON panels(part_id)");


%% Process panels

for k = 1:numel(partIds)

    partId = partIds(k);

    fprintf("\n========================================\n");
    fprintf("Panel %d / %d\n", k, numel(partIds));
    fprintf("%s\n", partId);
    fprintf("========================================\n");

    try

        %% -------------------------------------------------------------
        % 1. Apply panel dataset
        % --------------------------------------------------------------

        partrepo.collection.applyDataSet( ...
            block, ...
            collectionId, ...
            partId);


        %% -------------------------------------------------------------
        % 2. Get complete block dataset
        % --------------------------------------------------------------

        ds = partrepo.simscape.dataSetFromBlock(block);


        %% -------------------------------------------------------------
        % 3. Panel identity
        % --------------------------------------------------------------

        idParts = split(partId, "|");

        manufacturer = string(idParts(1));

        if numel(idParts) >= 2
            modelName = string(idParts(2));
        else
            modelName = "";
        end


        %% -------------------------------------------------------------
        % 4. Relevant Solar Cell parameters
        % --------------------------------------------------------------

        N_series = str2double( ...
            ds.Parameters.N_series.ValueExpr);

        N_parallel = str2double( ...
            ds.Parameters.N_parallel.ValueExpr);

        Rs = str2double( ...
            ds.Parameters.Rs.ValueExpr);

        Rp = str2double( ...
            ds.Parameters.Rp.ValueExpr);

        TFIXED = str2double( ...
            ds.Parameters.TFIXED.ValueExpr);


        %% -------------------------------------------------------------
        % 5. Generate Solar Cell characterization harness
        % --------------------------------------------------------------

        % Defensively clear any harness left over from a previous
        % iteration before asking for a fresh one -- if bdclose() below
        % didn't fully release it, a stale handle here can make the
        % next solarCellCharacteristicsHarness call reference an
        % invalid/half-torn-down model.
        if bdIsLoaded("SolarCellHarnessDigitalDatasheet")
            bdclose("SolarCellHarnessDigitalDatasheet");
        end

        modelOpen = false;

        modelname = ...
            ee.datasheet.sources.solarCellCharacteristicsHarness( ...
                block, modelOpen);


        %% -------------------------------------------------------------
        % 6. Generate reference rating
        % --------------------------------------------------------------

        solarCellData = ...
            ee.datasheet.sources.solarCellRating(modelname);


        %% -------------------------------------------------------------
        % 7. Temperature characterization
        % --------------------------------------------------------------

        temperatureData = ...
            ee.datasheet.sources.solarCellCharacteristicsPlot( ...
                modelname, ...
                solarCellData.Ir0, ...
                temperatureVec, ...
                solarCellData.Voc);


        %% -------------------------------------------------------------
        % 8. Irradiance characterization
        % --------------------------------------------------------------

        irradianceVec = ...
            irradianceFractions * solarCellData.Ir0;

        irradianceData = ...
            ee.datasheet.sources.solarCellCharacteristicsPlot( ...
                modelname, ...
                irradianceVec, ...
                solarCellData.Tmeas, ...
                solarCellData.Voc);


        %% -------------------------------------------------------------
        % 9. Close characterization harness
        % --------------------------------------------------------------

        if bdIsLoaded(modelname)
            bdclose(modelname);
        end


        %% -------------------------------------------------------------
        % 10. Insert panel row
        % --------------------------------------------------------------

        panelId = k;

        insertPanel(conn, panelId, partId, manufacturer, modelName, ...
            N_series, N_parallel, Rs, Rp, TFIXED, ...
            solarCellData.Vm, solarCellData.Im, solarCellData.Pm, ...
            solarCellData.Voc, solarCellData.Isc, solarCellData.Ir0, ...
            solarCellData.Tmeas);


        %% -------------------------------------------------------------
        % 11. Insert IV curve
        % --------------------------------------------------------------

        insertIVCurve(conn, panelId, ...
            solarCellData.V, solarCellData.I, solarCellData.P);


        %% -------------------------------------------------------------
        % 12. Insert temperature characterization
        % --------------------------------------------------------------

        insertCharacteristics(conn, ...
            "temperature_characteristics", panelId, temperatureData);


        %% -------------------------------------------------------------
        % 13. Insert irradiance characterization
        % --------------------------------------------------------------

        insertCharacteristics(conn, ...
            "irradiance_characteristics", panelId, irradianceData);


        %% -------------------------------------------------------------
        % 14. Close any datasheet figures
        % --------------------------------------------------------------
        close all force;

        fprintf("Successfully cached: %s\n", modelName);


    catch ME

        fprintf(2, ...
            "FAILED: %s\n", partId);

        fprintf(2, ...
            "%s\n", ME.message);

        % Make sure temporary models/figures do not accumulate
        close all;

        % Continue with the next panel
        continue;

    end

end

close(conn);


fprintf("\n========================================\n");
fprintf("Cache generation complete.\n");
fprintf("Output: %s\n", outputFile);
fprintf("========================================\n");


%% =====================================================================
% Local helper functions
% ======================================================================

function insertPanel(conn, id, partId, manufacturer, modelName, ...
        n_series, n_parallel, rs, rp, tfixed, ...
        vmp, imp, pmp, voc, isc, ir0, tmeas)
    % Parameterized insert -- values are bound directly (not serialized
    % to SQL text), so real Inf/NaN (e.g. Rp = Inf for panels with no
    % shunt leakage) pass through exactly as-is, same as the original
    % H5 cache and the todb.py-built SQLite mirror both already did.

    row = table( ...
        double(id), {char(partId)}, {char(manufacturer)}, {char(modelName)}, ...
        double(n_series), double(n_parallel), double(rs), double(rp), double(tfixed), ...
        double(vmp), double(imp), double(pmp), double(voc), double(isc), double(ir0), double(tmeas), ...
        'VariableNames', { ...
            'id', 'part_id', 'manufacturer', 'model_name', ...
            'n_series', 'n_parallel', 'rs', 'rp', 'tfixed', ...
            'vmp', 'imp', 'pmp', 'voc', 'isc', 'ir0', 'tmeas'});

    insert(conn, "panels", row.Properties.VariableNames, row);

end


function insertIVCurve(conn, panelId, V, I, P)

    V = double(V(:));
    I = double(I(:));
    P = double(P(:));
    n = numel(V);

    rows = table( ...
        repmat(double(panelId), n, 1), (1:n)', V, I, P, ...
        'VariableNames', {'panel_id', 'point', 'voltage', 'current', 'power'});

    insert(conn, "iv_curve", rows.Properties.VariableNames, rows);

end


function insertCharacteristics(conn, tableName, panelId, T)

    ir  = double(T.Ir_Wperm2(:));
    t   = double(T.T_degC(:));
    voc = double(T.Voc_Volts(:));
    isc = double(T.Isc_Ampere(:));
    vm  = double(T.Vm_Volts(:));
    im  = double(T.Im_Ampere(:));
    pm  = double(T.Pm_Watts(:));
    n   = numel(ir);

    rows = table( ...
        repmat(double(panelId), n, 1), ir, t, voc, isc, vm, im, pm, ...
        'VariableNames', { ...
            'panel_id', 'irradiance_wperm2', 'temperature_degC', ...
            'voc_volts', 'isc_ampere', 'vm_volts', 'im_ampere', 'pm_watts'});

    insert(conn, tableName, rows.Properties.VariableNames, rows);

end
