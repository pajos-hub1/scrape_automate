# Converted from: merge.ipynb

# ===== Cell 0 =====
# Cell 1: Import libraries
import pandas as pd
import glob
import re

# ===== Cell 1 =====
# Cell 2: Find all season files
# Find all Excel files matching the pattern "All_Rounds_Combined_s*.xlsx"
season_files = glob.glob("All_Rounds_Combined_s*.xlsx")

# Function to extract season number from filename
def extract_season_number(filename):
    """Extract season number from 'All_Rounds_Combined_s1.xlsx' -> 1"""
    match = re.search(r's(\d+)', filename)
    if match:
        return int(match.group(1))
    return 0

# Sort by season number
season_files.sort(key=extract_season_number)

print(f"Found {len(season_files)} season files:")
for i, file in enumerate(season_files, 1):
    season_num = extract_season_number(file)
    print(f"  {i}. {file} (Season {season_num})")

# ===== Cell 2 =====
# Cell 3: Load and merge all seasons
all_seasons_data = []

for file in season_files:
    # Extract season number
    season_num = extract_season_number(file)
    
    # Read the file
    df = pd.read_excel(file)
    
    # Add Season column
    df['Season'] = season_num
    
    # Add Season Label (optional, for better readability)
    df['Season Label'] = f'Season {season_num}'
    
    all_seasons_data.append(df)
    
    print(f"✓ Loaded {file}: Season {season_num}, {len(df)} matches")

print(f"\nTotal seasons loaded: {len(all_seasons_data)}")

# ===== Cell 3 =====
# Cell 4: Combine all seasons
# Combine all dataframes
combined_df = pd.concat(all_seasons_data, ignore_index=True)

# Reorder columns to put Season info first
cols = combined_df.columns.tolist()
# Move Season and Season Label to the front
if 'Season' in cols and 'Season Label' in cols:
    cols.remove('Season')
    cols.remove('Season Label')
    cols = ['Season', 'Season Label'] + cols

combined_df = combined_df[cols]

print(f"✓ Combined all seasons")
print(f"✓ Total matches: {len(combined_df)}")
print(f"✓ Seasons: {combined_df['Season'].min()} to {combined_df['Season'].max()}")
print(f"✓ Total columns: {len(combined_df.columns)}")

# ===== Cell 4 =====
# Cell 5: Preview combined data
print("First 10 rows:")
combined_df.head(10)

# ===== Cell 5 =====
# Cell 6: Summary by season
print("MATCHES PER SEASON:")
print("="*50)
season_summary = combined_df.groupby('Season').agg({
    'Round': 'nunique',  # Number of unique rounds
    'Team A': 'count'    # Total matches
}).rename(columns={'Round': 'Total Rounds', 'Team A': 'Total Matches'})

print(season_summary)
print(f"\n{'='*50}")
print(f"Total Matches Across All Seasons: {len(combined_df)}")

# ===== Cell 6 =====
# Cell 7: Check data quality
print("DATA QUALITY CHECK:")
print("="*50)

# Check for missing values
print("\nMissing values:")
missing = combined_df.isnull().sum()
missing = missing[missing > 0]
if len(missing) > 0:
    print(missing)
else:
    print("No missing values!")

# Check unique teams
print(f"\nUnique teams: {combined_df['Team A'].nunique() + combined_df['Team B'].nunique()}")
print(f"Total rounds: {combined_df['Round'].nunique()}")

# ===== Cell 7 =====
# Cell: Incremental Season Merge
import pandas as pd
import os

# Define the main combined file
main_file = 'All_Seasons_Combined.xlsx'

# Define the new season file to add
new_season_file = 'All_Rounds_Combined_s2.xlsx'  # Change this for each new season
new_season_number = 2  # Change this for each new season

# Check if main file exists
if os.path.exists(main_file):
    print(f"Loading existing data from {main_file}...")
    main_df = pd.read_excel(main_file)
    print(f"  Existing matches: {len(main_df)}")
    print(f"  Existing seasons: {main_df['Season'].unique().tolist()}")
else:
    print(f"{main_file} not found. Creating new combined file...")
    main_df = pd.DataFrame()

# Load new season
print(f"\nLoading new season from {new_season_file}...")
new_df = pd.read_excel(new_season_file)

# Add season information
new_df['Season'] = new_season_number
new_df['Season Label'] = f'Season {new_season_number}'

print(f"  New season matches: {len(new_df)}")

# Combine
if len(main_df) > 0:
    combined_df = pd.concat([main_df, new_df], ignore_index=True)
else:
    combined_df = new_df

# Save
combined_df.to_excel(main_file, index=False)

print(f"\n{'='*60}")
print(f"✓ Season {new_season_number} added successfully!")
print(f"✓ Total matches now: {len(combined_df)}")
print(f"✓ Total seasons: {combined_df['Season'].nunique()}")
print(f"✓ Updated: {main_file}")
print(f"{'='*60}")

# ===== Cell 8 =====
# Cell: Incremental Season Merge
import pandas as pd
import os

# Define the main combined file
main_file = 'All_Seasons_Combined.xlsx'

# Define the new season file to add
new_season_file = 'All_Rounds_Combined_s2.xlsx'  # Change this for each new season
new_season_number = 2  # Change this for each new season

# Check if main file exists
if os.path.exists(main_file):
    print(f"Loading existing data from {main_file}...")
    main_df = pd.read_excel(main_file)
    print(f"  Existing matches: {len(main_df)}")
    print(f"  Existing seasons: {main_df['Season'].unique().tolist()}")
else:
    print(f"{main_file} not found. Creating new combined file...")
    main_df = pd.DataFrame()

# Load new season
print(f"\nLoading new season from {new_season_file}...")
new_df = pd.read_excel(new_season_file)

# Add season information
new_df['Season'] = new_season_number
new_df['Season Label'] = f'Season {new_season_number}'

print(f"  New season matches: {len(new_df)}")

# Combine
if len(main_df) > 0:
    combined_df = pd.concat([main_df, new_df], ignore_index=True)
else:
    combined_df = new_df

# Save
combined_df.to_excel(main_file, index=False)

print(f"\n{'='*60}")
print(f"✓ Season {new_season_number} added successfully!")
print(f"✓ Total matches now: {len(combined_df)}")
print(f"✓ Total seasons: {combined_df['Season'].nunique()}")
print(f"✓ Updated: {main_file}")
print(f"{'='*60}")
