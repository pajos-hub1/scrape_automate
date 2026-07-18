# Converted from: Create Master Tracker.ipynb

# ===== Cell 0 =====
# Cell 13: Create Master Tracker - Combine All Rounds
import pandas as pd
import glob
import os
from datetime import datetime

print("="*80)
print("CREATING MASTER TRACKER")
print("="*80)

# Find all prediction files
prediction_files = glob.glob("Round_*_Predictions.xlsx")
prediction_files.sort()

print(f"\nFound {len(prediction_files)} prediction files:")
for file in prediction_files:
    print(f"  - {file}")

if len(prediction_files) == 0:
    print("\n⚠️ No prediction files found!")
    print("Make sure you have Round_XX_Predictions.xlsx files in the current directory")
else:
    # Read all prediction files
    all_rounds = []
    
    for file in prediction_files:
        try:
            # Read the predictions sheet
            df = pd.read_excel(file, sheet_name='Predictions')
            
            # Extract round number from filename
            round_num = int(file.split('_')[1])
            
            # Add round number if not present
            if 'Round Number' not in df.columns:
                df['Round Number'] = round_num
            
            # Add source file
            df['Source File'] = file
            
            all_rounds.append(df)
            print(f"✓ Loaded {file}: {len(df)} matches")
            
        except Exception as e:
            print(f"✗ Error loading {file}: {e}")
    
    if len(all_rounds) > 0:
        # Combine all rounds
        master_df = pd.concat(all_rounds, ignore_index=True)
        
        # Sort by round number and match number
        master_df = master_df.sort_values(['Round Number', 'Match #']).reset_index(drop=True)
        
        # Calculate overall statistics
        total_matches = len(master_df)
        rounds_tracked = master_df['Round Number'].nunique()
        
        # Count matches with actual results filled in
        matches_with_results = master_df['Actual Result'].notna().sum()
        
        # Create summary dataframe
        summary_data = {
            'Metric': [
                'Total Rounds Tracked',
                'Total Matches',
                'Matches with Results',
                'Matches Pending',
                'Data Coverage'
            ],
            'Value': [
                rounds_tracked,
                total_matches,
                matches_with_results,
                total_matches - matches_with_results,
                f"{(matches_with_results/total_matches)*100:.1f}%"
            ]
        }
        summary_df = pd.DataFrame(summary_data)
        
        # Calculate accuracy metrics (only for matches with results)
        completed_matches = master_df[master_df['Actual Result'].notna()].copy()
        
        if len(completed_matches) > 0:
            # Simple accuracy
            simple_correct = (completed_matches['Simple Prediction'] == completed_matches['Actual Result']).sum()
            simple_accuracy = (simple_correct / len(completed_matches)) * 100
            
            # ML accuracy
            ml_correct = (completed_matches['ML Prediction'] == completed_matches['Actual Result']).sum()
            ml_accuracy = (ml_correct / len(completed_matches)) * 100
            
            # Over 2.5 accuracy
            if 'Actual Over 2.5' in completed_matches.columns:
                over25_completed = completed_matches[completed_matches['Actual Over 2.5'].notna()]
                if len(over25_completed) > 0:
                    over25_correct = (over25_completed['Over 2.5'] == over25_completed['Actual Over 2.5']).sum()
                    over25_accuracy = (over25_correct / len(over25_completed)) * 100
                else:
                    over25_accuracy = 0
            else:
                over25_accuracy = 0
            
            # BTTS accuracy
            if 'Actual BTTS' in completed_matches.columns:
                btts_completed = completed_matches[completed_matches['Actual BTTS'].notna()]
                if len(btts_completed) > 0:
                    btts_correct = (btts_completed['BTTS'] == btts_completed['Actual BTTS']).sum()
                    btts_accuracy = (btts_correct / len(btts_completed)) * 100
                else:
                    btts_accuracy = 0
            else:
                btts_accuracy = 0
            
            # Add accuracy to summary
            accuracy_data = {
                'Metric': [
                    'Simple Model Accuracy',
                    'ML Model Accuracy',
                    'Over 2.5 Accuracy',
                    'BTTS Accuracy'
                ],
                'Value': [
                    f"{simple_accuracy:.1f}%",
                    f"{ml_accuracy:.1f}%",
                    f"{over25_accuracy:.1f}%",
                    f"{btts_accuracy:.1f}%"
                ]
            }
            accuracy_df = pd.DataFrame(accuracy_data)
            summary_df = pd.concat([summary_df, accuracy_df], ignore_index=True)
        
        # Round-by-round accuracy
        round_accuracy = []
        for round_num in sorted(completed_matches['Round Number'].unique()):
            round_data = completed_matches[completed_matches['Round Number'] == round_num]
            
            simple_acc = (round_data['Simple Prediction'] == round_data['Actual Result']).sum() / len(round_data) * 100
            ml_acc = (round_data['ML Prediction'] == round_data['Actual Result']).sum() / len(round_data) * 100
            
            round_accuracy.append({
                'Round': f"Round {round_num}",
                'Round Number': round_num,
                'Matches': len(round_data),
                'Simple Accuracy %': round(simple_acc, 1),
                'ML Accuracy %': round(ml_acc, 1)
            })
        
        round_accuracy_df = pd.DataFrame(round_accuracy)
        
        # Market performance comparison
        market_performance = []
        
        if len(completed_matches) > 0:
            # Match Result
            market_performance.append({
                'Market': 'Match Result (Simple)',
                'Predictions': len(completed_matches),
                'Correct': simple_correct,
                'Accuracy %': round(simple_accuracy, 1)
            })
            
            market_performance.append({
                'Market': 'Match Result (ML)',
                'Predictions': len(completed_matches),
                'Correct': ml_correct,
                'Accuracy %': round(ml_accuracy, 1)
            })
            
            # Over 2.5
            if 'Actual Over 2.5' in completed_matches.columns:
                over25_comp = completed_matches[completed_matches['Actual Over 2.5'].notna()]
                if len(over25_comp) > 0:
                    over25_corr = (over25_comp['Over 2.5'] == over25_comp['Actual Over 2.5']).sum()
                    market_performance.append({
                        'Market': 'Over 2.5 Goals',
                        'Predictions': len(over25_comp),
                        'Correct': over25_corr,
                        'Accuracy %': round((over25_corr/len(over25_comp))*100, 1)
                    })
            
            # BTTS
            if 'Actual BTTS' in completed_matches.columns:
                btts_comp = completed_matches[completed_matches['Actual BTTS'].notna()]
                if len(btts_comp) > 0:
                    btts_corr = (btts_comp['BTTS'] == btts_comp['Actual BTTS']).sum()
                    market_performance.append({
                        'Market': 'BTTS',
                        'Predictions': len(btts_comp),
                        'Correct': btts_corr,
                        'Accuracy %': round((btts_corr/len(btts_comp))*100, 1)
                    })
        
        market_performance_df = pd.DataFrame(market_performance)
        
        # Confidence analysis (for ML)
        confidence_analysis = []
        if len(completed_matches) > 0:
            # High confidence (70%+)
            high_conf = completed_matches[completed_matches['ML Confidence'] >= 70]
            if len(high_conf) > 0:
                high_correct = (high_conf['ML Prediction'] == high_conf['Actual Result']).sum()
                confidence_analysis.append({
                    'Confidence Level': 'High (70%+)',
                    'Predictions': len(high_conf),
                    'Correct': high_correct,
                    'Accuracy %': round((high_correct/len(high_conf))*100, 1)
                })
            
            # Medium confidence (50-70%)
            med_conf = completed_matches[(completed_matches['ML Confidence'] >= 50) & (completed_matches['ML Confidence'] < 70)]
            if len(med_conf) > 0:
                med_correct = (med_conf['ML Prediction'] == med_conf['Actual Result']).sum()
                confidence_analysis.append({
                    'Confidence Level': 'Medium (50-70%)',
                    'Predictions': len(med_conf),
                    'Correct': med_correct,
                    'Accuracy %': round((med_correct/len(med_conf))*100, 1)
                })
            
            # Low confidence (<50%)
            low_conf = completed_matches[completed_matches['ML Confidence'] < 50]
            if len(low_conf) > 0:
                low_correct = (low_conf['ML Prediction'] == low_conf['Actual Result']).sum()
                confidence_analysis.append({
                    'Confidence Level': 'Low (<50%)',
                    'Predictions': len(low_conf),
                    'Correct': low_correct,
                    'Accuracy %': round((low_correct/len(low_conf))*100, 1)
                })
        
        confidence_analysis_df = pd.DataFrame(confidence_analysis)
        
        # Save to Master Tracker Excel
        master_filename = 'Master_Tracker_Season_2.xlsx'
        
        with pd.ExcelWriter(master_filename, engine='openpyxl') as writer:
            # All predictions
            master_df.to_excel(writer, sheet_name='All Predictions', index=False)
            
            # Summary
            summary_df.to_excel(writer, sheet_name='Summary', index=False)
            
            # Round by round accuracy
            if len(round_accuracy_df) > 0:
                round_accuracy_df.to_excel(writer, sheet_name='Accuracy by Round', index=False)
            
            # Market performance
            if len(market_performance_df) > 0:
                market_performance_df.to_excel(writer, sheet_name='Market Performance', index=False)
            
            # Confidence analysis
            if len(confidence_analysis_df) > 0:
                confidence_analysis_df.to_excel(writer, sheet_name='Confidence Analysis', index=False)
        
        print(f"\n{'='*80}")
        print(f"✓ MASTER TRACKER CREATED")
        print(f"{'='*80}")
        print(f"File: {master_filename}")
        print(f"\nSheets created:")
        print(f"  1. All Predictions - {len(master_df)} matches")
        print(f"  2. Summary - Overall statistics")
        print(f"  3. Accuracy by Round - {len(round_accuracy_df)} rounds")
        print(f"  4. Market Performance - {len(market_performance_df)} markets")
        print(f"  5. Confidence Analysis - {len(confidence_analysis_df)} levels")
        
        if len(completed_matches) > 0:
            print(f"\nCurrent Performance:")
            print(f"  Simple Model: {simple_accuracy:.1f}%")
            print(f"  ML Model: {ml_accuracy:.1f}%")
            print(f"  Matches Analyzed: {len(completed_matches)}")
        
        print(f"{'='*80}\n")
        
        # Store for dashboard
        master_data = {
            'master_df': master_df,
            'completed_matches': completed_matches,
            'round_accuracy_df': round_accuracy_df,
            'market_performance_df': market_performance_df,
            'confidence_analysis_df': confidence_analysis_df
        }

# ===== Cell 1 =====
# Cell 14: Create Performance Dashboard with Charts
import matplotlib.pyplot as plt
import seaborn as sns
from openpyxl import load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.utils.dataframe import dataframe_to_rows

# Set style for matplotlib charts
plt.style.use('seaborn-v0_8-darkgrid')
sns.set_palette("husl")

print("="*80)
print("CREATING PERFORMANCE DASHBOARD")
print("="*80)

# Check if we have data
if 'master_data' not in locals():
    print("\n⚠️ No master data found!")
    print("Please run Cell 13 (Master Tracker) first")
else:
    # Extract data
    master_df = master_data['master_df']
    completed_matches = master_data['completed_matches']
    round_accuracy_df = master_data['round_accuracy_df']
    market_performance_df = master_data['market_performance_df']
    confidence_analysis_df = master_data['confidence_analysis_df']
    
    if len(completed_matches) == 0:
        print("\n⚠️ No completed matches found!")
        print("Fill in actual results in your prediction files first")
    else:
        # Create visualizations
        fig = plt.figure(figsize=(16, 12))
        
        # 1. Accuracy Over Time (Line Chart)
        if len(round_accuracy_df) > 0:
            ax1 = plt.subplot(3, 3, 1)
            ax1.plot(round_accuracy_df['Round Number'], round_accuracy_df['Simple Accuracy %'], 
                    marker='o', label='Simple Model', linewidth=2, markersize=8)
            ax1.plot(round_accuracy_df['Round Number'], round_accuracy_df['ML Accuracy %'], 
                    marker='s', label='ML Model', linewidth=2, markersize=8)
            ax1.axhline(y=50, color='r', linestyle='--', alpha=0.5, label='50% Baseline')
            ax1.set_xlabel('Round Number', fontsize=10)
            ax1.set_ylabel('Accuracy %', fontsize=10)
            ax1.set_title('Accuracy Trend Over Time', fontsize=12, fontweight='bold')
            ax1.legend()
            ax1.grid(True, alpha=0.3)
        
        # 2. Market Performance Comparison (Bar Chart)
        if len(market_performance_df) > 0:
            ax2 = plt.subplot(3, 3, 2)
            markets = market_performance_df['Market']
            accuracies = market_performance_df['Accuracy %']
            bars = ax2.barh(markets, accuracies, color=sns.color_palette("husl", len(markets)))
            ax2.axvline(x=50, color='r', linestyle='--', alpha=0.5, label='50% Baseline')
            ax2.set_xlabel('Accuracy %', fontsize=10)
            ax2.set_title('Performance by Market', fontsize=12, fontweight='bold')
            ax2.set_xlim([0, 100])
            
            # Add value labels on bars
            for i, bar in enumerate(bars):
                width = bar.get_width()
                ax2.text(width + 2, bar.get_y() + bar.get_height()/2, 
                        f'{width:.1f}%', ha='left', va='center', fontsize=9)
            ax2.grid(True, alpha=0.3, axis='x')
        
        # 3. Confidence Analysis (Bar Chart)
        if len(confidence_analysis_df) > 0:
            ax3 = plt.subplot(3, 3, 3)
            conf_levels = confidence_analysis_df['Confidence Level']
            conf_acc = confidence_analysis_df['Accuracy %']
            bars = ax3.bar(range(len(conf_levels)), conf_acc, 
                          color=sns.color_palette("RdYlGn", len(conf_levels)))
            ax3.axhline(y=50, color='r', linestyle='--', alpha=0.5)
            ax3.set_xticks(range(len(conf_levels)))
            ax3.set_xticklabels(conf_levels, rotation=45, ha='right')
            ax3.set_ylabel('Accuracy %', fontsize=10)
            ax3.set_title('ML Model Accuracy by Confidence', fontsize=12, fontweight='bold')
            ax3.set_ylim([0, 100])
            
            # Add value labels
            for i, bar in enumerate(bars):
                height = bar.get_height()
                ax3.text(bar.get_x() + bar.get_width()/2, height + 2,
                        f'{height:.1f}%', ha='center', va='bottom', fontsize=9)
            ax3.grid(True, alpha=0.3, axis='y')
        
        # 4. Prediction Distribution (Pie Charts)
        ax4 = plt.subplot(3, 3, 4)
        simple_dist = completed_matches['Simple Prediction'].value_counts()
        colors = ['#ff9999', '#66b3ff', '#99ff99']
        ax4.pie(simple_dist.values, labels=simple_dist.index, autopct='%1.1f%%',
               colors=colors, startangle=90)
        ax4.set_title('Simple Model Predictions', fontsize=12, fontweight='bold')
        
        ax5 = plt.subplot(3, 3, 5)
        ml_dist = completed_matches['ML Prediction'].value_counts()
        ax5.pie(ml_dist.values, labels=ml_dist.index, autopct='%1.1f%%',
               colors=colors, startangle=90)
        ax5.set_title('ML Model Predictions', fontsize=12, fontweight='bold')
        
        # 5. Actual Results Distribution
        ax6 = plt.subplot(3, 3, 6)
        actual_dist = completed_matches['Actual Result'].value_counts()
        ax6.pie(actual_dist.values, labels=actual_dist.index, autopct='%1.1f%%',
               colors=colors, startangle=90)
        ax6.set_title('Actual Results', fontsize=12, fontweight='bold')
        
        # 6. Model Agreement Analysis
        ax7 = plt.subplot(3, 3, 7)
        agreement = (completed_matches['Simple Prediction'] == completed_matches['ML Prediction']).sum()
        disagreement = len(completed_matches) - agreement
        ax7.bar(['Models Agree', 'Models Disagree'], [agreement, disagreement], 
               color=['#90ee90', '#ffcccb'])
        ax7.set_ylabel('Number of Matches', fontsize=10)
        ax7.set_title('Simple vs ML Agreement', fontsize=12, fontweight='bold')
        ax7.grid(True, alpha=0.3, axis='y')
        
        # Add value labels
        for i, v in enumerate([agreement, disagreement]):
            ax7.text(i, v + 0.5, str(v), ha='center', va='bottom', fontsize=10, fontweight='bold')
        
        # 7. Cumulative Accuracy
        if len(round_accuracy_df) > 0:
            ax8 = plt.subplot(3, 3, 8)
            # Calculate cumulative accuracy
            cumulative_simple = []
            cumulative_ml = []
            total_correct_simple = 0
            total_correct_ml = 0
            total_matches = 0
            
            for idx, row in round_accuracy_df.iterrows():
                total_matches += row['Matches']
                total_correct_simple += (row['Simple Accuracy %'] / 100) * row['Matches']
                total_correct_ml += (row['ML Accuracy %'] / 100) * row['Matches']
                
                cumulative_simple.append((total_correct_simple / total_matches) * 100)
                cumulative_ml.append((total_correct_ml / total_matches) * 100)
            
            ax8.plot(round_accuracy_df['Round Number'], cumulative_simple, 
                    marker='o', label='Simple Model', linewidth=2)
            ax8.plot(round_accuracy_df['Round Number'], cumulative_ml, 
                    marker='s', label='ML Model', linewidth=2)
            ax8.axhline(y=50, color='r', linestyle='--', alpha=0.5)
            ax8.set_xlabel('Round Number', fontsize=10)
            ax8.set_ylabel('Cumulative Accuracy %', fontsize=10)
            ax8.set_title('Cumulative Accuracy', fontsize=12, fontweight='bold')
            ax8.legend()
            ax8.grid(True, alpha=0.3)
        
        # 8. Win/Draw/Away Distribution Comparison
        ax9 = plt.subplot(3, 3, 9)
        
        # Count predictions and actuals
        categories = ['Home Win', 'Draw', 'Away Win']
        simple_counts = [completed_matches[completed_matches['Simple Prediction'] == cat].shape[0] for cat in categories]
        ml_counts = [completed_matches[completed_matches['ML Prediction'] == cat].shape[0] for cat in categories]
        actual_counts = [completed_matches[completed_matches['Actual Result'] == cat].shape[0] for cat in categories]
        
        x = range(len(categories))
        width = 0.25
        
        ax9.bar([i - width for i in x], simple_counts, width, label='Simple', alpha=0.8)
        ax9.bar([i for i in x], ml_counts, width, label='ML', alpha=0.8)
        ax9.bar([i + width for i in x], actual_counts, width, label='Actual', alpha=0.8)
        
        ax9.set_xlabel('Outcome', fontsize=10)
        ax9.set_ylabel('Count', fontsize=10)
        ax9.set_title('Prediction vs Actual Distribution', fontsize=12, fontweight='bold')
        ax9.set_xticks(x)
        ax9.set_xticklabels(categories)
        ax9.legend()
        ax9.grid(True, alpha=0.3, axis='y')
        
        plt.tight_layout()
        
        # Save the figure
        dashboard_image = 'Performance_Dashboard.png'
        plt.savefig(dashboard_image, dpi=300, bbox_inches='tight')
        print(f"\n✓ Dashboard visualization saved: {dashboard_image}")
        
        plt.show()
        
        # Create Excel Dashboard with embedded charts
        dashboard_filename = 'Performance_Dashboard_Season_2.xlsx'
        
        # Copy master tracker and add dashboard sheet
        master_filename = 'Master_Tracker_Season_2.xlsx'
        
        # Read master tracker
        with pd.ExcelWriter(dashboard_filename, engine='openpyxl') as writer:
            # Write all the analysis data
            round_accuracy_df.to_excel(writer, sheet_name='Accuracy Trend', index=False)
            market_performance_df.to_excel(writer, sheet_name='Market Performance', index=False)
            confidence_analysis_df.to_excel(writer, sheet_name='Confidence Analysis', index=False)
            
            # Create summary dashboard sheet
            summary_data = []
            
            # Overall stats
            total_predictions = len(completed_matches)
            simple_correct = (completed_matches['Simple Prediction'] == completed_matches['Actual Result']).sum()
            ml_correct = (completed_matches['ML Prediction'] == completed_matches['Actual Result']).sum()
            
            summary_data.append(['OVERALL PERFORMANCE', ''])
            summary_data.append(['Total Predictions', total_predictions])
            summary_data.append(['Simple Model Accuracy', f"{(simple_correct/total_predictions)*100:.1f}%"])
            summary_data.append(['ML Model Accuracy', f"{(ml_correct/total_predictions)*100:.1f}%"])
            summary_data.append(['', ''])
            
            # Best/Worst rounds
            if len(round_accuracy_df) > 0:
                best_round_simple = round_accuracy_df.loc[round_accuracy_df['Simple Accuracy %'].idxmax()]
                worst_round_simple = round_accuracy_df.loc[round_accuracy_df['Simple Accuracy %'].idxmin()]
                
                summary_data.append(['SIMPLE MODEL', ''])
                summary_data.append(['Best Round', f"Round {best_round_simple['Round Number']} ({best_round_simple['Simple Accuracy %']:.1f}%)"])
                summary_data.append(['Worst Round', f"Round {worst_round_simple['Round Number']} ({worst_round_simple['Simple Accuracy %']:.1f}%)"])
                summary_data.append(['', ''])
                
                best_round_ml = round_accuracy_df.loc[round_accuracy_df['ML Accuracy %'].idxmax()]
                worst_round_ml = round_accuracy_df.loc[round_accuracy_df['ML Accuracy %'].idxmin()]
                
                summary_data.append(['ML MODEL', ''])
                summary_data.append(['Best Round', f"Round {best_round_ml['Round Number']} ({best_round_ml['ML Accuracy %']:.1f}%)"])
                summary_data.append(['Worst Round', f"Round {worst_round_ml['Round Number']} ({worst_round_ml['ML Accuracy %']:.1f}%)"])
            
            summary_df = pd.DataFrame(summary_data, columns=['Metric', 'Value'])
            summary_df.to_excel(writer, sheet_name='Dashboard Summary', index=False)
        
        print(f"✓ Dashboard Excel created: {dashboard_filename}")
        
        print(f"\n{'='*80}")
        print(f"✓ PERFORMANCE DASHBOARD CREATED")
        print(f"{'='*80}")
        print(f"\nFiles created:")
        print(f"  1. {dashboard_image} - Visual charts")
        print(f"  2. {dashboard_filename} - Excel dashboard")
        print(f"\nKey Insights:")
        print(f"  Total Predictions: {total_predictions}")
        print(f"  Simple Accuracy: {(simple_correct/total_predictions)*100:.1f}%")
        print(f"  ML Accuracy: {(ml_correct/total_predictions)*100:.1f}%")
        
        if len(round_accuracy_df) > 0:
            avg_simple = round_accuracy_df['Simple Accuracy %'].mean()
            avg_ml = round_accuracy_df['ML Accuracy %'].mean()
            print(f"  Average Simple: {avg_simple:.1f}%")
            print(f"  Average ML: {avg_ml:.1f}%")
            
            # Trend analysis
            if len(round_accuracy_df) >= 3:
                recent_3_simple = round_accuracy_df.tail(3)['Simple Accuracy %'].mean()
                recent_3_ml = round_accuracy_df.tail(3)['ML Accuracy %'].mean()
                print(f"\n  Recent Trend (last 3 rounds):")
                print(f"    Simple: {recent_3_simple:.1f}%")
                print(f"    ML: {recent_3_ml:.1f}%")
        
        print(f"{'='*80}\n")
        
## What These Create:

### **Master Tracker (Cell 13):**
# Creates `Master_Tracker_Season_2.xlsx` with 5 sheets:

# 1. **All Predictions** - Every match from every round
# 2. **Summary** - Overall statistics
# 3. **Accuracy by Round** - Round-by-round performance
# 4. **Market Performance** - Match Result vs Over/Under vs BTTS
# 5. **Confidence Analysis** - High/Medium/Low confidence performance

# ### **Performance Dashboard (Cell 14):**
# Creates 2 files:

# 1. **Performance_Dashboard.png** - Beautiful visualizations:
#    - Accuracy trend over time (line chart)
#    - Market performance comparison (bar chart)
#    - Confidence level analysis
#    - Prediction distributions (pie charts)
#    - Model agreement analysis
#    - Cumulative accuracy
#    - Win/Draw/Away distribution

# 2. **Performance_Dashboard_Season_2.xlsx** - Excel with:
#    - Dashboard summary
#    - All analysis tables
#    - Key insights

# ## When to Run:

# **Master Tracker:** Run after every 5-10 rounds
# **Dashboard:** Run after Master Tracker

# ## Usage Flow:
# 1. Make predictions → Save Round_XX_Predictions.xlsx
# 2. After round finishes → Fill in actual results
# 3. Repeat for 5-10 rounds
# 4. Run Cell 13 (Master Tracker)
# 5. Run Cell 14 (Performance Dashboard)
# 6. Analyze results and improve model!
