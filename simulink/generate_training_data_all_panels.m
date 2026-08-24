%% generate_training_data_all_panels.m

clc;
clear;

%% ================================================================
% 0. Resolve paths relative to this script's own location
% ================================================================

scriptDir = fileparts(mfilename('fullpath'));

%% ================================================================
% 1. Import Open-Meteo weather data
% ================================================================

weatherFile = fullfile(scriptDir, '..', 'data', 'raw', 'weather_data.csv');

data = readtable(weatherFile, ...
    'VariableNamingRule', 'preserve');

data.time = datetime(data.time, ...
    'InputFormat', 'yyyy-MM-dd''T''HH:mm');

% Ensure chronological ordering
data = sortrows(data, 'time');

% Simulation time in seconds
sim_time_sec = seconds(data.time - data.time(1));

% Simulink inputs
ghi_ts = timeseries( ...
    data.("shortwave_radiation (W/m²)"), ...
    sim_time_sec);

temp_ts = timeseries( ...
    data.("temperature_2m (°C)"), ...
    sim_time_sec);


%% ================================================================
% 2. SQLite panel cache
% ================================================================

DB_FILE = fullfile(scriptDir, '..', 'data', 'raw', 'panel_cache.db');

OUTPUT_FILE = fullfile(scriptDir, '..', 'data', 'simulated', 'training_data_all_panels.csv');


%% ================================================================
% 3. Get all cached panels
% ================================================================

dbConn = sqlite(DB_FILE);

panelsTable = fetch(dbConn, [ ...
    "SELECT id, part_id, manufacturer, model_name, " + ...
    "n_series, n_parallel, vmp, imp, pmp, voc, isc " + ...
    "FROM panels ORDER BY id"]);

numPanels = height(panelsTable);
numRows = height(data);

fprintf('\n============================================\n');
fprintf('Panels found:    %d\n', numPanels);
fprintf('Weather rows:    %d\n', numRows);
fprintf('Expected rows:   %d\n', numPanels * numRows);
fprintf('============================================\n\n');


%% ================================================================
% 4. Load Simulink model
% Loaded by full path (not bare name) to avoid ambiguity if another
% file named PVOutputSimulation2025a.slx exists elsewhere on the
% MATLAB path.
% ================================================================

load_system(fullfile(scriptDir, 'PVOutputSimulation2025a.slx'));

solarBlock = 'PVOutputSimulation2025a/Solar Cell';


%% ================================================================
% 5. Remove previous output
% ================================================================

if isfile(OUTPUT_FILE)
    delete(OUTPUT_FILE);
end


%% ================================================================
% 6. Process every panel
% ================================================================

for panelIndex = 1:numPanels

    panelRow = panelsTable(panelIndex, :);
    panelId = panelRow.id;

    fprintf('\n[%d/%d] Processing panel_id=%d\n', ...
        panelIndex, numPanels, panelId);


    %% ------------------------------------------------------------
    % Read panel identity
    % ------------------------------------------------------------

    partId = string(panelRow.part_id);

    manufacturer = string(panelRow.manufacturer);

    modelName = string(panelRow.model_name);


    %% ------------------------------------------------------------
    % Read rated specifications
    % ------------------------------------------------------------

    PmpRated = panelRow.pmp;

    VocRated = panelRow.voc;

    IscRated = panelRow.isc;

    VmpRated = panelRow.vmp;

    ImpRated = panelRow.imp;

    N_series = panelRow.n_series;

    N_parallel = panelRow.n_parallel;


    %% ------------------------------------------------------------
    % Load this panel's temperature lookup
    % ------------------------------------------------------------

    tempLookupTable = fetch(dbConn, sprintf( ...
        "SELECT temperature_degC, vm_volts FROM temperature_characteristics " + ...
        "WHERE panel_id = %d ORDER BY temperature_degC", panelId));

    temperaturePoints = tempLookupTable.temperature_degC;
    vmpPoints = tempLookupTable.vm_volts;

    temperaturePoints = temperaturePoints(:);
    vmpPoints = vmpPoints(:);

    % Variables referenced directly by Simulink
    temperatureLookup = temperaturePoints;
    vmpLookup = vmpPoints;


    %% ------------------------------------------------------------
    % Apply this panel's Simscape dataset
    % ------------------------------------------------------------

    partrepo.collection.applyDataSet( ...
        solarBlock, ...
        "SimscapeElectrical", ...
        partId);


    %% ------------------------------------------------------------
    % Run simulation
    % ------------------------------------------------------------

    fprintf('    Running simulation: %s\n', modelName);

    out = sim('PVOutputSimulation2025a');


    %% ------------------------------------------------------------
    % Extract simulation outputs
    % ------------------------------------------------------------

    simVoltage = out.sim_voltage(:);
    simCurrent = out.sim_current(:);
    simPower   = out.sim_power(:);


    %% ------------------------------------------------------------
    % Validate output
    % ------------------------------------------------------------

    if numel(simPower) ~= numRows

        error( ...
            ['Panel %s produced %d simulation rows, but ' ...
             'weather data contains %d rows.'], ...
            partId, numel(simPower), numRows);

    end


    %% ------------------------------------------------------------
    % Create this panel's training rows
    % ------------------------------------------------------------

    panelData = table( ...
        data.time, ...
        data.("temperature_2m (°C)"), ...
        data.("shortwave_radiation (W/m²)"), ...
        data.("relative_humidity_2m (%)"), ...
        data.("cloud_cover (%)"), ...
        data.("wind_speed_10m (km/h)"), ...
        data.("precipitation (mm)"), ...
        data.("dew_point_2m (°C)"), ...
        repmat(manufacturer, numRows, 1), ...
        repmat(modelName, numRows, 1), ...
        repmat(partId, numRows, 1), ...
        repmat(PmpRated, numRows, 1), ...
        repmat(VocRated, numRows, 1), ...
        repmat(IscRated, numRows, 1), ...
        repmat(VmpRated, numRows, 1), ...
        repmat(ImpRated, numRows, 1), ...
        repmat(N_series, numRows, 1), ...
        repmat(N_parallel, numRows, 1), ...
        simVoltage, ...
        simCurrent, ...
        simPower, ...
        'VariableNames', { ...
            'time', ...
            'temperature_2m_c', ...
            'shortwave_radiation_wm2', ...
            'relative_humidity_pct', ...
            'cloud_cover_pct', ...
            'wind_speed_10m_kmh', ...
            'precipitation_mm', ...
            'dew_point_2m_c', ...
            'manufacturer', ...
            'model_name', ...
            'part_id', ...
            'rated_pmax_w', ...
            'rated_voc_v', ...
            'rated_isc_a', ...
            'rated_vmp_v', ...
            'rated_imp_a', ...
            'n_series', ...
            'n_parallel', ...
            'output_voltage_v', ...
            'output_current_a', ...
            'output_power_w'});


    %% ------------------------------------------------------------
    % Append to CSV
    % ------------------------------------------------------------

    if panelIndex == 1

        writetable(panelData, OUTPUT_FILE);

    else

        writetable(panelData, OUTPUT_FILE, ...
            'WriteMode', 'append');

    end


    %% ------------------------------------------------------------
    % Progress information
    % ------------------------------------------------------------

    [maximumPower, idx] = max(simPower);

    fprintf('    Manufacturer:   %s\n', manufacturer);
    fprintf('    Model:          %s\n', modelName);
    fprintf('    Rated Pmax:     %.4f W\n', PmpRated);
    fprintf('    Simulated Pmax: %.4f W\n', maximumPower);
    fprintf('    Rows written:   %d\n', numRows);

end

close(dbConn);


%% ================================================================
% 7. Final chronological sort
% ================================================================

fprintf('\nSorting final dataset chronologically...\n');

trainingData = readtable(OUTPUT_FILE, ...
    'VariableNamingRule', 'preserve');

trainingData.time = datetime(trainingData.time);

trainingData = sortrows(trainingData, 'time');

writetable(trainingData, OUTPUT_FILE);


%% ================================================================
% 8. Final report
% ================================================================

fprintf('\n============================================\n');
fprintf('ALL PANELS COMPLETE\n');
fprintf('--------------------------------------------\n');
fprintf('Panels processed: %d\n', numPanels);
fprintf('Rows per panel:   %d\n', numRows);
fprintf('Total rows:       %d\n', height(trainingData));
fprintf('First timestamp:  %s\n', ...
    string(trainingData.time(1)));
fprintf('Last timestamp:   %s\n', ...
    string(trainingData.time(end)));
fprintf('Output:\n%s\n', OUTPUT_FILE);
fprintf('============================================\n');